import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

from pydantic import BaseModel, ValidationError

from fastapi_canon.integrations.websockets._activity import _Activity, notify_activity
from fastapi_canon.integrations.websockets._adapter import adapter
from fastapi_canon.integrations.websockets.codec import RpcCodec
from fastapi_canon.integrations.websockets.envelopes import (
    RpcErrorData,
    RpcFailure,
    RpcNotification,
    RpcRequestEnvelope,
    RpcSuccess,
)
from fastapi_canon.integrations.websockets.errors import RpcError, RpcInvalidParamsError
from fastapi_canon.integrations.websockets.middleware import (
    RpcRequestOutcome,
    RpcRequestScope,
)
from fastapi_canon.integrations.websockets.protocol import RpcSubscriptionDefinition
from fastapi_canon.integrations.websockets.server import RpcResponse, RpcServer

logger: logging.Logger = logging.getLogger(__name__)


class RpcSubscriptionLimitError(RpcError):
    code = "subscription_limit"
    message = "Too many subscriptions"


class SubscriptionSession:
    async def wait_finished(self) -> None:
        await asyncio.gather(
            *(task for _, task in tuple(self._tasks.values())), return_exceptions=True
        )

    def __init__(
        self,
        definitions: tuple[RpcSubscriptionDefinition, ...],
        resolver: Any,
        send: Callable[[str], Awaitable[None]],
        *,
        limit: int,
        server: RpcServer,
        activity: _Activity | None = None,
    ) -> None:
        self._definitions = {item.name: item for item in definitions}
        self._methods = frozenset(
            f"{name}.{suffix}"
            for name in self._definitions
            for suffix in ("subscribe", "unsubscribe")
        )
        self._resolver = resolver
        self._send = send
        self._limit = limit
        self._activity = activity
        self._server = server
        self._next_id = 1
        self._tasks: dict[str, tuple[str, asyncio.Task[None]]] = {}
        self._pending_subscriptions = 0
        self._codec = RpcCodec()
        self._request: ContextVar[RpcRequestScope | None] = ContextVar(
            "fastapi_canon.integrations.websockets.subscription.request", default=None
        )
        self._responded: ContextVar[bool] = ContextVar(
            "fastapi_canon.integrations.websockets.subscription.responded",
            default=False,
        )

    async def handle(
        self, request: RpcRequestEnvelope, *, admitted_at: float | None = None
    ) -> RpcResponse | None:
        async def handshake(scope: "RpcRequestScope | None") -> None:
            token = self._request.set(scope)
            try:
                await self._handle(request)
            finally:
                self._request.reset(token)

        token = self._responded.set(False)
        try:
            response = await self._server._run_bounded(
                request.model_dump(exclude_unset=True),
                handshake,
                admitted_at=admitted_at,
            )
            # Middleware teardown can time out after the handshake is committed.
            return None if self._responded.get() else response
        finally:
            self._responded.reset(token)

    async def _handle(self, request: RpcRequestEnvelope) -> None:
        if not request.expects_response:
            return
        if isinstance(request.params, list):
            await self._respond(
                request,
                RpcFailure.from_error(
                    request.id,
                    RpcInvalidParamsError(message="Subscriptions require named params"),
                ),
            )
            return
        name = request.method
        if name.endswith(".subscribe"):
            definition = self._definitions[name[: -len(".subscribe")]]
            try:
                if len(self._tasks) + self._pending_subscriptions >= self._limit:
                    raise RpcSubscriptionLimitError()
                params = _params(definition, request.params)
            except RpcError as error:
                scope = self._request.get()
                if scope is not None:
                    scope.outcome = RpcRequestOutcome(None, 0.0, error)
                await self._respond(request, RpcFailure.from_error(request.id, error))
                return
            subscription_id = str(self._next_id)
            self._next_id += 1
            self._pending_subscriptions += 1
            try:
                await self._respond(
                    request,
                    RpcSuccess(
                        id=request.id, result={"subscriptionId": subscription_id}
                    ),
                )
            finally:
                self._pending_subscriptions -= 1
            task = asyncio.create_task(self._run(definition, subscription_id, params))
            self._tasks[subscription_id] = (definition.name, task)
            task.add_done_callback(lambda _: self._tasks.pop(subscription_id, None))
            return
        subscription_id = request.params.get("subscriptionId")
        if (
            not isinstance(subscription_id, str)
            or subscription_id not in self._tasks
            or self._tasks[subscription_id][0] != name[: -len(".unsubscribe")]
        ):
            await self._respond(
                request,
                RpcFailure.from_error(
                    request.id,
                    RpcInvalidParamsError(message="Unknown subscriptionId"),
                ),
            )
            return
        _, task = self._tasks[subscription_id]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await self._respond(request, RpcSuccess(id=request.id, result=None))

    def owns(self, method: str) -> bool:
        return method in self._methods

    async def close(self) -> None:
        tasks = tuple(task for _, task in self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _respond(self, request: RpcRequestEnvelope, response: Any) -> None:
        scope = self._request.get()
        if scope is not None:
            scope.outcome = RpcRequestOutcome(
                response,
                0.0,
                error=None if scope.outcome is None else scope.outcome.error,
                error_code=str(response.error.code)
                if isinstance(response, RpcFailure)
                else None,
            )
        if request.expects_response:
            await self._send(self._codec.encode(response))
            self._responded.set(True)

    async def _run(
        self,
        definition: RpcSubscriptionDefinition,
        subscription_id: str,
        params: BaseModel | None,
    ) -> None:
        generator = None
        try:
            async with definition.resolver_scope(self._resolver) as resolver:
                arguments = {
                    item.name: await resolver.resolve(item.dependency)
                    for item in definition.injected_parameters
                }
                if definition.params_parameter is not None:
                    arguments[definition.params_parameter] = params
                generator = definition.function(**arguments)
                async for payload in generator:
                    value = adapter(definition.payload).validate_python(payload)
                    message = self._codec.encode(
                        RpcNotification(
                            method=definition.name,
                            params={
                                "subscriptionId": subscription_id,
                                "payload": adapter(definition.payload).dump_python(
                                    value, mode="json", by_alias=True
                                ),
                            },
                        )
                    )
                    await self._send_notification(definition.name, message)
            await self._terminal(definition.name, subscription_id, complete=True)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            failure = self._server.failure(
                None, error, method=definition.name, declared=definition.raises
            )
            await self._terminal(
                definition.name, subscription_id, complete=False, error=failure.error
            )
        finally:
            if generator is not None:
                await generator.aclose()

    async def _terminal(
        self,
        name: str,
        subscription_id: str,
        *,
        complete: bool,
        error: RpcErrorData | None = None,
    ) -> None:
        await self._send_notification(
            name,
            self._codec.encode(
                RpcNotification(
                    method=name,
                    params={
                        "subscriptionId": subscription_id,
                        "complete": complete,
                        **(
                            {}
                            if error is None
                            else {
                                "error": error.model_dump(
                                    mode="json", exclude_none=True
                                )
                            }
                        ),
                    },
                )
            ),
        )

    async def _send_notification(self, name: str, message: str) -> None:
        await self._send(message)
        await notify_activity(
            self._activity, "notification_sent", name, len(message.encode())
        )


def _params(
    definition: RpcSubscriptionDefinition, raw: dict[str, Any]
) -> BaseModel | None:
    if definition.params is None:
        if raw:
            raise RpcInvalidParamsError(message="Subscription takes no params")
        return None
    try:
        validated = adapter(definition.params).validate_python(raw)
        if (
            definition.params_origin is not None
            and type(validated) is not definition.params_origin
        ):
            return definition.params_origin.model_validate(
                validated.model_dump(by_alias=False), by_name=True
            )
        return validated
    except ValidationError as error:
        raise RpcInvalidParamsError.from_validation_error(error) from error
