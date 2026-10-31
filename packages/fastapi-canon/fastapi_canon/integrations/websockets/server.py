import asyncio
import logging
import time
from asyncio import timeout_at
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager, nullcontext
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from fastapi_canon.integrations.websockets.codec import RpcCodec
from fastapi_canon.integrations.websockets.connection import RpcConnection, RpcLimits
from fastapi_canon.integrations.websockets.constants import LOGGER_NAME
from fastapi_canon.integrations.websockets.dependencies import RpcResolver
from fastapi_canon.integrations.websockets.dispatch import RpcDispatcher
from fastapi_canon.integrations.websockets.envelopes import (
    RpcFailure,
    RpcRequestEnvelope,
    RpcRequestId,
    RpcSuccess,
)
from fastapi_canon.integrations.websockets.errors import (
    RpcError,
    RpcInternalError,
    RpcInvalidRequestError,
    RpcParseError,
    RpcRequestCancelledError,
    RpcRequestTimeoutError,
    bind_contracts,
    contract_for,
    contract_of,
)
from fastapi_canon.integrations.websockets.middleware import (
    RpcMiddlewareLike,
    RpcRequestContext,
    RpcRequestOutcome,
    RpcRequestScope,
    _middleware_scope,
)
from fastapi_canon.integrations.websockets.protocol import (
    RpcMethodDefinition,
    RpcProtocol,
)
from fastapi_canon.integrations.websockets.request_metadata import (
    observed_request_metadata,
)

if TYPE_CHECKING:
    from fastapi_canon.integrations.websockets.service import RpcEndpoint

type RpcErrorMapper = Callable[[Exception], RpcError | None]
type RpcResponse = RpcSuccess | RpcFailure
type RpcResponseMessage = RpcResponse | list[RpcResponse] | None

logger: logging.Logger = logging.getLogger(LOGGER_NAME)
_CLIENT_CANCEL = object()


class RpcServer:
    """Serve decoded or encoded JSON-RPC messages over any transport."""

    def __init__(self) -> None:
        raise TypeError("RpcServer instances are created by RpcChannel.create_server()")

    @classmethod
    def _from_channel(
        cls,
        protocol: RpcProtocol,
        *,
        resolver: RpcResolver | None = None,
        error_mapper: RpcErrorMapper | None = None,
        connection: RpcConnection | None = None,
        limits: RpcLimits | None = None,
        strict_errors: bool = False,
        middleware: Sequence[RpcMiddlewareLike] = (),
        endpoint: "RpcEndpoint[Any] | None" = None,
    ) -> "RpcServer":
        server = cls.__new__(cls)
        server._protocol = protocol
        server._dispatcher = RpcDispatcher(protocol, resolver=resolver)
        server._error_mapper = error_mapper
        server._middleware = tuple(middleware)
        server._endpoint = endpoint
        server._connection = connection
        server._codec = RpcCodec()
        server._limits = limits or RpcLimits()
        server._semaphore = asyncio.Semaphore(server._limits.max_concurrency)
        server._strict_errors = strict_errors
        server._methods = {method.name: method for method in protocol.methods}
        server._scope_methods = (
            {method.name: method for method in protocol.methods}
            if server._middleware
            else {}
        )
        for subscription in protocol.subscriptions if server._middleware else ():
            for suffix in ("subscribe", "unsubscribe"):
                name = f"{subscription.name}.{suffix}"
                server._scope_methods[name] = RpcMethodDefinition(
                    name=name,
                    handler_name=name,
                    request_name=name,
                    params=subscription.params if suffix == "subscribe" else None,
                    result=Any,
                    raises=subscription.raises,
                )
        return server

    @property
    def protocol(self) -> RpcProtocol:
        return self._protocol

    async def handle(self, raw_request: object) -> RpcResponseMessage:
        """Serve one decoded request or batch."""
        if isinstance(raw_request, list):
            if not raw_request or len(raw_request) > self._limits.max_batch_size:
                return await self._handle_failure(raw_request, RpcInvalidRequestError())
            responses = await asyncio.gather(
                *(self._handle_one_bounded(item) for item in raw_request)
            )
            return [response for response in responses if response is not None] or None
        return await self._handle_one_bounded(raw_request)

    async def _handle_one_bounded(
        self,
        raw_request: object,
        *,
        admitted_at: float | None = None,
        envelope: RpcRequestEnvelope | None = None,
    ) -> RpcResponse | None:
        async def dispatch(scope: "RpcRequestScope | None") -> RpcResponse | None:
            return await self._dispatch_request(
                raw_request, scope=scope, envelope=envelope
            )

        return await self._run_bounded(raw_request, dispatch, admitted_at=admitted_at)

    def _request_timeout(self, raw_request: object) -> float | None:
        method = self._methods.get(_request_method(raw_request) or "")
        if method is not None and method._has_timeout:
            return method.timeout
        return self._limits.request_timeout

    async def _run_bounded(
        self,
        raw_request: object,
        operation: Callable[[RpcRequestScope | None], Awaitable[RpcResponse | None]],
        *,
        admitted_at: float | None = None,
    ) -> RpcResponse | None:
        timeout = self._request_timeout(raw_request)
        started = (
            asyncio.get_running_loop().time() if admitted_at is None else admitted_at
        )
        deadline = None if timeout is None else started + timeout
        waiting = timeout_at(deadline) if deadline is not None else None

        def interruption(error: BaseException) -> RpcError | None:
            if not isinstance(error, asyncio.CancelledError):
                return None
            if waiting is not None and waiting.expired():
                return RpcRequestTimeoutError()
            if (
                error.args
                and error.args[0] is _CLIENT_CANCEL
                and (self._connection is None or self._connection.close_code is None)
            ):
                return RpcRequestCancelledError()
            return None

        observed = False
        response: RpcResponse | None = None

        def on_error(scope: RpcRequestScope, caught: BaseException) -> None:
            nonlocal observed, response
            if (error := interruption(caught)) is not None:
                response = self._failure_response(raw_request, error, scope=scope)
                observed = True

        try:
            async with (
                waiting if waiting is not None else nullcontext(),
                (
                    self._request_scope(raw_request, on_error=on_error)
                    if self._middleware
                    else nullcontext(None)
                ) as scope,
                self._semaphore,
            ):
                return await operation(scope)
        except TimeoutError:
            if waiting is None or not waiting.expired():
                raise
            failure = RpcRequestTimeoutError()
        except asyncio.CancelledError as cancelled:
            failure = interruption(cancelled)
            if failure is None:
                raise
        return response if observed else self._failure_response(raw_request, failure)

    async def handle_json(self, message: str | bytes | bytearray) -> str | None:
        """Decode, serve, and encode one JSON-RPC message."""
        try:
            request = self._codec.decode(message)
        except RpcParseError as error:
            return self._codec.encode(await self._handle_failure(message, error))
        response = await self.handle(request)
        return None if response is None else self._codec.encode(response)

    @asynccontextmanager
    async def _request_scope(
        self,
        raw_request: object,
        *,
        on_error: Callable[[RpcRequestScope, BaseException], None] | None = None,
    ) -> AsyncGenerator[RpcRequestScope, None]:
        request = RpcRequestContext(
            raw_request=raw_request,
            method=_request_method(raw_request),
            request_id=_request_id(raw_request),
            notification=_looks_like_notification(raw_request),
            connection=self._connection,
            metadata=observed_request_metadata(raw_request),
        )
        scope = RpcRequestScope(
            request,
            self._endpoint,
            self._scope_methods.get(request.method or ""),
        )
        started = time.perf_counter()
        caught: BaseException | None = None

        def interrupted(error: BaseException) -> None:
            if on_error is not None:
                on_error(scope, error)
            if scope.outcome is not None:
                scope.outcome = replace(
                    scope.outcome, duration=time.perf_counter() - started
                )

        async with _middleware_scope(
            self._middleware, "request", scope, on_error=interrupted
        ):
            try:
                yield scope
            except BaseException as error:
                caught = error
                raise
            finally:
                previous = scope.outcome
                error = caught if previous is None else previous.error or caught
                scope.outcome = RpcRequestOutcome(
                    response=None if previous is None else previous.response,
                    duration=time.perf_counter() - started,
                    error=error,
                    cancelled=isinstance(caught, asyncio.CancelledError)
                    or (previous is not None and previous.cancelled),
                    error_code=None if previous is None else previous.error_code,
                )

    async def _handle_one(
        self, raw_request: object, *, failure: Exception | None = None
    ) -> RpcResponse | None:
        if not self._middleware:
            return await self._dispatch_request(raw_request, failure=failure)
        async with self._request_scope(raw_request) as scope:
            return await self._dispatch_request(
                raw_request, failure=failure, scope=scope
            )

    async def _dispatch_request(
        self,
        raw_request: object,
        *,
        failure: Exception | None = None,
        scope: RpcRequestScope | None = None,
        envelope: RpcRequestEnvelope | None = None,
    ) -> RpcResponse | None:
        invocation = None
        response: RpcResponse | None = None
        try:
            if failure is not None:
                raise failure
            invocation = self._dispatcher.parse_request(raw_request, envelope=envelope)
            result = await self._dispatcher.execute(invocation)
        except Exception as error:
            return self._failure_response(
                raw_request,
                error,
                scope=scope,
                declared=invocation.method.raises if invocation else (),
            )
        else:
            response = (
                RpcSuccess._with_result_annotation(
                    invocation.request.id, result, invocation.method.result
                )
                if invocation.request.expects_response
                else None
            )
            if scope is not None:
                scope.outcome = RpcRequestOutcome(response, 0.0)
        return response

    def _failure_response(
        self,
        raw_request: object,
        error: Exception,
        *,
        scope: RpcRequestScope | None = None,
        declared: tuple[type[RpcError], ...] = (),
    ) -> RpcFailure | None:
        mapped = self._rpc_error(
            error,
            _request_method(raw_request) if scope is None else scope.request.method,
            declared=declared,
        )
        notification = (
            _looks_like_notification(raw_request)
            if scope is None
            else scope.request.notification
        )
        response = (
            None
            if notification
            else RpcFailure.from_error(
                _request_id(raw_request) if scope is None else scope.request.request_id,
                mapped,
            )
        )
        if scope is not None:
            scope.outcome = RpcRequestOutcome(
                response,
                0.0,
                error,
                cancelled=isinstance(error, RpcRequestCancelledError),
                error_code=str(mapped.rpc_code),
            )
        return response

    async def _handle_failure(
        self, raw_request: object, error: Exception
    ) -> RpcResponse | None:
        return await self._handle_one(raw_request, failure=error)

    def failure(
        self,
        request_id: RpcRequestId,
        error: Exception,
        *,
        method: str | None = None,
        declared: tuple[type[RpcError], ...] = (),
    ) -> RpcFailure:
        rpc_error = self._rpc_error(error, method, declared=declared)
        return RpcFailure.from_error(request_id, rpc_error)

    def _rpc_error(
        self,
        error: Exception,
        method: str | None = None,
        *,
        declared: tuple[type[RpcError], ...] = (),
    ) -> RpcError:
        if isinstance(error, RpcError):
            mapped = error
        elif (
            contract := contract_for(
                error,
                bind_contracts(
                    contract
                    for declared_error in declared
                    if (contract := contract_of(declared_error)) is not None
                ),
            )
        ) is not None:
            try:
                mapped = contract.to_error(error)
            except Exception:
                logger.exception(
                    "RPC error binding %s failed for method %s", contract.code, method
                )
                return RpcInternalError()
        else:
            try:
                mapped = (
                    self._error_mapper(error)
                    if self._error_mapper is not None
                    else None
                )
                if mapped is not None and not isinstance(mapped, RpcError):
                    raise TypeError("RPC error mapper must return RpcError or None")
            except Exception:
                logger.exception("RPC error mapper failed for method %s", method)
                return RpcInternalError()
        if mapped is not None:
            if (
                self._strict_errors
                and method is not None
                and not mapped._builtin
                and not isinstance(mapped, declared)
            ):
                logger.error(
                    "RPC method %s raised undeclared error %s",
                    method,
                    type(mapped).__name__,
                )
                return RpcInternalError()
            return mapped
        logger.error("RPC method %s failed", method, exc_info=error)
        return RpcInternalError()


def _request_id(raw_request: object) -> RpcRequestId:
    if not isinstance(raw_request, dict):
        return None
    request_id = raw_request.get("id")
    if isinstance(request_id, bool):
        return None
    return request_id if isinstance(request_id, str | int) else None


def _request_method(raw_request: object) -> str | None:
    if not isinstance(raw_request, dict):
        return None
    method = raw_request.get("method")
    return method if isinstance(method, str) else None


def _looks_like_notification(raw_request: object) -> bool:
    return (
        isinstance(raw_request, dict)
        and "id" not in raw_request
        and raw_request.get("jsonrpc") == "2.0"
        and isinstance(raw_request.get("method"), str)
    )
