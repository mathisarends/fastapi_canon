import asyncio
import logging
import time
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager, nullcontext, suppress
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from fastapi_canon.integrations.websockets._activity import _Activity, notify_activity
from fastapi_canon.integrations.websockets._adapter import adapter
from fastapi_canon.integrations.websockets.codec import RpcCodec
from fastapi_canon.integrations.websockets.connection import (
    _STARTUP_EXPIRED,
    REJECTION_CLOSES,
    RpcBeforeAccept,
    RpcConnection,
    RpcConnectionClose,
    RpcConnectionExpiry,
    RpcDisconnect,
    RpcLimits,
    RpcReject,
    RpcRejection,
    RpcRejections,
    RpcWebSocket,
    rejection_for,
)
from fastapi_canon.integrations.websockets.constants import LOGGER_NAME
from fastapi_canon.integrations.websockets.dependencies import (
    RpcResolverLike,
    as_resolver,
    connection_scope,
    context_values,
)
from fastapi_canon.integrations.websockets.drain import _current_drain_session
from fastapi_canon.integrations.websockets.envelopes import (
    RpcFailure,
    RpcNotification,
    RpcRequestEnvelope,
    RpcSuccess,
)
from fastapi_canon.integrations.websockets.errors import (
    RpcError,
    RpcInternalError,
    RpcInvalidRequestError,
    RpcParseError,
    RpcRequestCancelledError,
    RpcServerShuttingDownError,
    bind_contracts,
    contract_for,
    contract_of,
)
from fastapi_canon.integrations.websockets.middleware import (
    RpcMiddlewareLike,
    RpcStreamScope,
    _current_serving,
    _record_error,
    _record_rejection,
    _serving_scope,
    _stream_scope,
)
from fastapi_canon.integrations.websockets.server import (
    _CLIENT_CANCEL,
    RpcErrorMapper,
    RpcServer,
)
from fastapi_canon.integrations.websockets.service import (
    RpcEndpoint,
    RpcSocketEndpoint,
    RpcStreamEndpoint,
)
from fastapi_canon.integrations.websockets.sockets import RpcMessageType, RpcSocket
from fastapi_canon.integrations.websockets.streams import (
    RpcBinaryInput,
    RpcBinaryOutput,
    RpcInputEndMessage,
    RpcStreamClose,
)
from fastapi_canon.integrations.websockets.subscriptions import SubscriptionSession

logger: logging.Logger = logging.getLogger(LOGGER_NAME)


def _rejection(error: Exception, rejections: RpcRejections | None) -> RpcReject | None:
    try:
        return rejection_for(error, rejections)
    except Exception:
        logger.exception("RPC rejection mapper failed")
        return None


async def _prepare(
    endpoint,
    socket,
    resolver,
    context,
    path_model=None,
    before_accept=None,
    rejections=None,
):
    endpoint.service.freeze()
    resolved = as_resolver(resolver)
    values = context_values(context)
    path_params = (
        socket.handshake.path_params or endpoint.match(socket.handshake.path) or {}
    )
    connection = RpcConnection._create(endpoint, socket, path_params)
    drain_session = _current_drain_session()
    if drain_session is not None:
        drain_session.connection = connection
        if not isinstance(endpoint, RpcEndpoint):
            drain_session.ready.set()
    state = _current_serving()
    if state is not None:
        state.scope.connection = connection
    base_values = {**values, RpcConnection: connection}
    if (
        endpoint.subprotocol is not None
        and endpoint.subprotocol not in socket.handshake.subprotocols
    ):
        _record_rejection(RpcRejection.PROTOCOL_ERROR)
        await socket.reject(RpcRejection.PROTOCOL_ERROR, "Unsupported subprotocol")
        return None
    path_values: dict[str, Any] = {}
    if path_model is not None:
        try:
            parsed = path_model.model_validate(dict(path_params))
        except ValidationError:
            _record_rejection(RpcRejection.NOT_FOUND)
            await socket.reject(RpcRejection.NOT_FOUND, "Invalid path variable")
            return None
        path_values = {name: getattr(parsed, name) for name in path_model.model_fields}
        base_values[path_model] = parsed
    if before_accept is not None:
        try:
            accepted_values = await before_accept(socket.handshake)
        except Exception as error:
            _record_error(error)
            rejected = _rejection(error, rejections)
            if rejected is None:
                logger.exception("RPC before_accept hook failed")
                rejected = RpcReject(RpcRejection.INTERNAL_ERROR, "Internal error")
            _record_rejection(rejected.rejection)
            await socket.reject(
                rejected.rejection, rejected.reason, headers=rejected.headers
            )
            return None
        if accepted_values is not None:
            base_values.update(accepted_values)
    expiry = base_values.get(RpcConnectionExpiry)
    if isinstance(expiry, RpcConnectionExpiry):
        connection.expire_at(expiry.expires_at)
    if endpoint.service.drain.draining:
        _record_rejection(RpcRejection.UNAVAILABLE)
        await socket.reject(RpcRejection.UNAVAILABLE, "Server restarting")
        return None
    await socket.accept(endpoint.subprotocol)
    connection._accepted = True
    connection._arm_expiry()
    if state is not None:
        state.accepted_at = time.perf_counter()
    return connection, resolved, base_values, path_values


async def serve_endpoint(
    endpoint: RpcEndpoint,
    socket: RpcWebSocket,
    *,
    resolver: RpcResolverLike | None = None,
    context: object | Mapping[type[Any], object] | None = None,
    error_mapper: RpcErrorMapper | None = None,
    limits: RpcLimits | None = None,
    before_accept: RpcBeforeAccept | None = None,
    rejections: RpcRejections | None = None,
    middleware: Sequence[RpcMiddlewareLike] = (),
) -> None:
    async with (
        endpoint.service.drain._session(socket) as session,
        _serving_scope(endpoint, socket, middleware) as state,
    ):
        if session is None:
            _record_rejection(RpcRejection.UNAVAILABLE)
            return
        await _serve_endpoint(
            endpoint,
            socket,
            resolver=resolver,
            context=context,
            error_mapper=error_mapper,
            limits=limits,
            before_accept=before_accept,
            rejections=rejections,
            activity=None if state is None else state.activity,
            middleware=() if state is None else state.middleware,
        )


async def _serve_endpoint(
    endpoint: RpcEndpoint,
    socket: RpcWebSocket,
    *,
    resolver: RpcResolverLike | None = None,
    context: object | Mapping[type[Any], object] | None = None,
    error_mapper: RpcErrorMapper | None = None,
    limits: RpcLimits | None = None,
    before_accept: RpcBeforeAccept | None = None,
    rejections: RpcRejections | None = None,
    activity: _Activity | None = None,
    middleware: Sequence[RpcMiddlewareLike] = (),
) -> None:
    limits = limits or RpcLimits()
    prepared = await _prepare(
        endpoint,
        socket,
        resolver,
        context,
        endpoint.path_model,
        before_accept,
        rejections,
    )
    if prepared is None:
        return
    connection, resolved, values, _ = prepared
    async with _accepted(socket, connection):
        await notify_activity(activity, "connection_opened", connection)
        await _run_rpc_endpoint(
            endpoint,
            socket,
            connection,
            resolved,
            values,
            error_mapper,
            limits,
            rejections,
            activity,
            middleware,
        )


async def _run_rpc_endpoint(
    endpoint,
    socket,
    connection,
    resolved,
    values,
    error_mapper,
    limits,
    rejections,
    activity,
    middleware,
):
    close_event = asyncio.Event()
    close_value = [RpcConnectionClose.NORMAL, ""]
    client_closed = False
    outgoing: asyncio.Queue[str] = asyncio.Queue(limits.max_queue_size)
    slow_consumer_reported = False
    pending_rejections = 0
    pending = _PendingRequests(limits.max_pending_requests)

    async def report_slow_consumer() -> None:
        nonlocal slow_consumer_reported
        if slow_consumer_reported:
            return
        slow_consumer_reported = True
        await notify_activity(activity, "slow_consumer_closed", connection)

    async def send_outgoing(message: str) -> None:
        try:
            async with asyncio.timeout(limits.send_timeout):
                await outgoing.put(message)
        except TimeoutError:
            logger.warning("RPC client too slow: outgoing queue blocked")
            request_close(RpcConnectionClose.POLICY_VIOLATION, "Client too slow")
            await report_slow_consumer()

    codec = RpcCodec()

    def request_close(code, reason, raw_close_code=None):
        if not close_event.is_set():
            close_value[:] = [code, reason]
            connection._close_code = code
            connection._close_reason = reason
            connection._raw_close_code = raw_close_code
            close_event.set()

    connection._set_close_handler(request_close)
    tasks: set[asyncio.Task] = set()
    call_tasks: set[asyncio.Task] = set()
    requests: dict[tuple[type, str | int], _PendingCall] = {}
    try:
        async with connection_scope(resolved, values) as scoped:
            server = RpcServer._from_channel(
                endpoint.protocol,
                resolver=scoped,
                error_mapper=error_mapper,
                middleware=middleware,
                endpoint=endpoint,
                connection=connection,
                limits=limits,
                strict_errors=endpoint.service.strict_errors,
            )
            subscriptions = SubscriptionSession(
                endpoint.protocol.subscriptions,
                scoped,
                send_outgoing,
                limit=limits.max_subscriptions,
                server=server,
                activity=activity,
            )

            async def writer():
                nonlocal client_closed
                try:
                    while True:
                        message = await outgoing.get()
                        try:
                            async with asyncio.timeout(limits.send_timeout):
                                await socket.send(message)
                        finally:
                            outgoing.task_done()
                except asyncio.CancelledError:
                    raise
                except RpcDisconnect as error:
                    client_closed = True
                    request_close(error.code, error.reason, error.raw_close_code)
                except TimeoutError as error:
                    _record_error(error)
                    logger.warning("RPC client too slow: socket send blocked")
                    request_close(
                        RpcConnectionClose.POLICY_VIOLATION, "Client too slow"
                    )
                    await report_slow_consumer()
                except Exception as error:
                    _record_error(error)
                    logger.exception("RPC writer failed")
                    request_close(RpcConnectionClose.INTERNAL_ERROR, "Internal error")

            async def invoke_one(call):
                call.started = True
                message = call.message
                try:
                    if call.invalid:
                        return await server._handle_failure(
                            message, RpcInvalidRequestError()
                        )
                    if call.shutting_down:
                        return await server._handle_failure(
                            message, RpcServerShuttingDownError()
                        )
                    if call.reservation is None:
                        return await reject_pending(message)
                    if not call.batch and subscriptions.owns(message["method"]):
                        return await subscriptions.handle(
                            call.envelope, admitted_at=call.admitted_at
                        )
                    return await server._handle_one_bounded(
                        message, admitted_at=call.admitted_at, envelope=call.envelope
                    )
                finally:
                    if call.reservation is not None:
                        pending.release(call.reservation)

            async def collect_one(call):
                try:
                    return await call.task
                except asyncio.CancelledError:
                    # Task.cancel() can prevent an admitted task from ever starting.
                    if (
                        call.started
                        or not call.cancel_requested
                        or close_event.is_set()
                        or asyncio.current_task().cancelling()
                    ):
                        raise
                    return await server._handle_failure(
                        call.message, RpcRequestCancelledError()
                    )

            async def invoke(items, batch, controls=()):
                try:
                    responses = (
                        [await collect_one(items[0])]
                        if len(items) == 1
                        else await asyncio.gather(
                            *(collect_one(call) for call in items)
                        )
                    )
                finally:
                    unfinished = [call.task for call in items if not call.task.done()]
                    for task in unfinished:
                        task.cancel()
                    if unfinished:
                        await asyncio.gather(*unfinished, return_exceptions=True)
                responses = [*controls, *(r for r in responses if r is not None)]
                if responses and not close_event.is_set():
                    await send_outgoing(
                        codec.encode(responses if batch else responses[0])
                    )

            async def reject_pending(message):
                nonlocal pending_rejections
                pending_rejections += 1
                if pending_rejections == 1:
                    logger.warning("RPC client exceeded max_pending_requests")
                return await server._handle_failure(message, RpcPendingLimitError())

            def invocation_done(task):
                tasks.discard(task)
                if not task.cancelled() and (error := task.exception()) is not None:
                    _record_error(error)
                    request_close(RpcConnectionClose.INTERNAL_ERROR, "Internal error")

            def schedule_failure(message, error):
                if len(tasks) >= limits.max_pending_requests + limits.max_queue_size:
                    request_close(
                        RpcConnectionClose.POLICY_VIOLATION,
                        "Too many response collectors",
                    )
                    return False

                async def respond():
                    response = await server._handle_failure(message, error)
                    if response is not None and not close_event.is_set():
                        await send_outgoing(codec.encode(response))

                task = asyncio.create_task(respond())
                tasks.add(task)
                task.add_done_callback(invocation_done)
                return True

            def call_done(call, key):
                call_tasks.discard(call.task)
                if call.reservation is not None:
                    pending.release(call.reservation)
                if key is not None and requests.get(key) is call:
                    del requests[key]

            def cancel_request(item):
                if (
                    not isinstance(item, dict)
                    or item.get("method") != "$/cancelRequest"
                ):
                    return False
                if "id" in item:
                    return False
                params = item.get("params")
                if (
                    item.get("jsonrpc") == "2.0"
                    and set(item) == {"jsonrpc", "method", "params"}
                    and isinstance(params, dict)
                    and set(params) == {"id"}
                    and type(params["id"]) in (str, int)
                ):
                    call = requests.get((type(params["id"]), params["id"]))
                    if (
                        call is not None
                        and call.reservation is not None
                        and not call.cancel_requested
                        and not call.task.done()
                    ):
                        call.cancel_requested = True
                        call.task.cancel(_CLIENT_CANCEL)
                return True

            async def reader():
                nonlocal client_closed
                try:
                    while True:
                        frame = await socket.receive()
                        size = len(
                            frame if isinstance(frame, bytes) else frame.encode()
                        )
                        if (
                            limits.max_message_bytes is not None
                            and size > limits.max_message_bytes
                        ):
                            request_close(RpcConnectionClose.MESSAGE_TOO_BIG, "")
                            return
                        if isinstance(frame, bytes):
                            try:
                                frame = frame.decode()
                            except UnicodeDecodeError:
                                request_close(
                                    RpcConnectionClose.PROTOCOL_ERROR,
                                    "Invalid RPC frame",
                                )
                                return
                        try:
                            message = codec.decode(frame)
                        except RpcParseError as error:
                            if not schedule_failure(frame, error):
                                return
                            continue
                        batch = isinstance(message, list)
                        if batch and (
                            not message or len(message) > limits.max_batch_size
                        ):
                            if not schedule_failure(message, RpcInvalidRequestError()):
                                return
                            continue
                        items = []
                        controls = []
                        # Bound response collectors as well as admitted items.
                        collectors_full = len(tasks) >= (
                            limits.max_pending_requests + limits.max_queue_size
                        )
                        for item in message if batch else [message]:
                            if cancel_request(item):
                                continue
                            if (
                                isinstance(item, dict)
                                and item.get("method") == "$/ping"
                            ):
                                valid = (
                                    item.get("jsonrpc") == "2.0"
                                    and set(item)
                                    <= {"jsonrpc", "method", "id", "params"}
                                    and item.get("params", {}) == {}
                                    and (
                                        "id" not in item
                                        or type(item["id"]) in (str, int)
                                    )
                                )
                                if "id" in item:
                                    response = (
                                        RpcSuccess(id=item["id"], result=None)
                                        if valid
                                        else RpcFailure.from_error(
                                            None, RpcInvalidRequestError()
                                        )
                                    )
                                    controls.append(response)
                                continue
                            if collectors_full:
                                request_close(
                                    RpcConnectionClose.POLICY_VIOLATION,
                                    "Too many response collectors",
                                )
                                return
                            try:
                                envelope = RpcRequestEnvelope.model_validate(item)
                                if envelope.method == "$/cancelRequest":
                                    raise ValueError(
                                        "Cancellation must be a notification"
                                    )
                            except (ValidationError, ValueError):
                                envelope = None
                                invalid = True
                                reservation = None
                            else:
                                invalid = False
                                reservation = (
                                    None
                                    if endpoint.service.drain.draining
                                    else pending.reserve()
                                )
                            key = None
                            if not invalid and type(item.get("id")) in (str, int):
                                key = (type(item["id"]), item["id"])
                                if key in requests:
                                    if reservation is not None:
                                        pending.release(reservation)
                                    request_close(
                                        RpcConnectionClose.PROTOCOL_ERROR,
                                        "Duplicate in-flight request id",
                                    )
                                    return
                            call = _PendingCall(
                                item,
                                reservation,
                                invalid,
                                batch,
                                asyncio.get_running_loop().time(),
                                shutting_down=not invalid
                                and endpoint.service.drain.draining,
                                envelope=envelope,
                            )
                            call.task = asyncio.create_task(invoke_one(call))
                            call_tasks.add(call.task)
                            if key is not None:
                                requests[key] = call
                            call.task.add_done_callback(
                                lambda _, call=call, key=key: call_done(call, key)
                            )
                            items.append(call)
                        if not items and not controls:
                            continue
                        if len(tasks) >= (
                            limits.max_pending_requests + limits.max_queue_size
                        ):
                            request_close(
                                RpcConnectionClose.POLICY_VIOLATION,
                                "Too many response collectors",
                            )
                            return
                        task = asyncio.create_task(invoke(items, batch, controls))
                        tasks.add(task)
                        task.add_done_callback(invocation_done)
                except RpcDisconnect as error:
                    client_closed = True
                    request_close(error.code, error.reason, error.raw_close_code)
                except Exception as error:
                    _record_error(error)
                    request_close(RpcConnectionClose.INTERNAL_ERROR, "Internal error")

            async def events():
                try:
                    await asyncio.gather(
                        *(
                            _event_source(
                                event,
                                scoped,
                                send_outgoing,
                                activity,
                                rejections,
                                server,
                            )
                            for event in endpoint.protocol.notifications
                        )
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    _record_error(error)
                    rejected = _rejection(error, rejections)
                    if rejected is not None:
                        request_close(
                            REJECTION_CLOSES[rejected.rejection], rejected.reason
                        )
                        return
                    logger.exception("RPC event source failed")
                    request_close(RpcConnectionClose.INTERNAL_ERROR, "Internal error")
                    return
                if endpoint.close_when_events_complete:
                    request_close(RpcConnectionClose.NORMAL, "")

            writer_task = asyncio.create_task(writer())
            producers = [
                asyncio.create_task(reader()),
                asyncio.create_task(events()),
            ]

            async def graceful():
                producers[1].cancel()
                await asyncio.gather(producers[1], return_exceptions=True)
                await asyncio.gather(*tuple(tasks), return_exceptions=True)
                await subscriptions.wait_finished()
                producers[0].cancel()
                await asyncio.gather(
                    producers[0], *tuple(tasks), return_exceptions=True
                )
                await _flush(outgoing, writer_task, None)
                request_close(RpcConnectionClose.SERVICE_RESTART, "Server restarting")

            drain_session = _current_drain_session()
            if drain_session is not None:
                drain_session.begin = graceful
                drain_session.ready.set()
            try:
                connection._startup_task = None
                await close_event.wait()
                for task in (*producers, *tasks, *call_tasks):
                    task.cancel()
                await asyncio.gather(
                    *producers, *tasks, *call_tasks, return_exceptions=True
                )
                await subscriptions.close()
                if close_value[0] in (
                    RpcConnectionClose.NORMAL,
                    RpcConnectionClose.SHUTDOWN,
                ):
                    await _flush(outgoing, writer_task, limits.send_timeout)
            finally:
                for task in (writer_task, *producers, *tasks, *call_tasks):
                    task.cancel()
                try:
                    await asyncio.gather(
                        writer_task,
                        *producers,
                        *tasks,
                        *call_tasks,
                        return_exceptions=True,
                    )
                finally:
                    await subscriptions.close()
    except asyncio.CancelledError:
        shutdown = connection.close_code or RpcConnectionClose.SHUTDOWN
        close_value[:] = [shutdown, connection.close_reason]
        connection._close_code = shutdown
        if not client_closed:
            with suppress(RpcDisconnect):
                await socket.close(*close_value)
        connection._mark_closed()
        raise
    finally:
        if pending_rejections:
            logger.debug(
                "Rejected %d messages exceeding max_pending_requests "
                "on this connection",
                pending_rejections,
            )
        if not client_closed and not connection.closed:
            with suppress(RpcDisconnect):
                await socket.close(*close_value)
        connection._mark_closed()


@dataclass
class _PendingCall:
    message: Any
    reservation: object | None
    invalid: bool
    batch: bool
    admitted_at: float
    shutting_down: bool = False
    # Validated once by the reader and reused for dispatch.
    envelope: RpcRequestEnvelope | None = None
    task: asyncio.Task = field(init=False)
    started: bool = False
    cancel_requested: bool = False


class _PendingRequests:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._reservations: set[object] = set()

    @property
    def count(self) -> int:
        return len(self._reservations)

    def reserve(self) -> object | None:
        if self.count >= self.limit:
            return None
        reservation = object()
        self._reservations.add(reservation)
        return reservation

    def release(self, reservation: object) -> None:
        self._reservations.discard(reservation)


class RpcPendingLimitError(RpcError):
    _builtin = True
    code = "pending_limit"
    message = "Too many pending requests"


async def _flush(
    outgoing: asyncio.Queue[str], writer: asyncio.Task, timeout: float | None
) -> None:
    drained = asyncio.create_task(outgoing.join())
    try:
        await asyncio.wait(
            (drained, writer), timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        drained.cancel()


async def _event_source(event, resolver, send, activity, rejections, server):
    try:
        arguments = {
            parameter.name: await resolver.resolve(parameter.dependency)
            for parameter in event.injected_parameters
        }
        payload_adapter = adapter(event.payload)
        codec = RpcCodec()
        async for payload in event.function(**arguments):
            try:
                value = payload_adapter.validate_python(payload)
                message = codec.encode(
                    RpcNotification._with_payload_annotation(
                        event.name, value, event.payload
                    )
                )
            except Exception:
                logger.exception("RPC event %s produced an invalid payload", event.name)
                if event.on_error == "close":
                    raise
                continue
            await send(message)
            await notify_activity(
                activity, "notification_sent", event.name, len(message.encode())
            )
    except asyncio.CancelledError:
        raise
    except Exception as error:
        binding = contract_for(
            error,
            bind_contracts(
                binding
                for declared in event.raises
                if (binding := contract_of(declared)) is not None
            ),
        )
        if binding is not None:
            try:
                rejected = binding.reject(error)
            except Exception:
                logger.exception("RPC event error binding failed for %s", event.name)
                raise RpcReject(
                    RpcRejection.INTERNAL_ERROR, "Internal error"
                ) from error
            if rejected is not None:
                raise rejected from error
        _record_error(error)
        rejected = _rejection(error, rejections)
        if rejected is not None:
            raise rejected from error
        mapped = server._rpc_error(error, event.name, declared=event.raises)
        if event.on_error == "close":
            rejection = (
                RpcRejection.INTERNAL_ERROR
                if isinstance(mapped, RpcInternalError)
                else RpcRejection.FORBIDDEN
            )
            raise RpcReject(rejection, mapped.message) from error


async def serve_stream_endpoint(
    endpoint: RpcStreamEndpoint,
    socket: RpcWebSocket,
    *,
    resolver: RpcResolverLike | None = None,
    context: object | Mapping[type[Any], object] | None = None,
    limits: RpcLimits | None = None,
    before_accept: RpcBeforeAccept | None = None,
    error_mapper: RpcErrorMapper | None = None,
    rejections: RpcRejections | None = None,
    middleware: Sequence[RpcMiddlewareLike] = (),
) -> None:
    async with (
        endpoint.service.drain._session(socket) as session,
        _serving_scope(endpoint, socket, middleware) as state,
    ):
        if session is None:
            _record_rejection(RpcRejection.UNAVAILABLE)
            return
        await _serve_stream_endpoint(
            endpoint,
            socket,
            resolver=resolver,
            context=context,
            error_mapper=error_mapper,
            limits=limits,
            before_accept=before_accept,
            rejections=rejections,
            activity=None if state is None else state.activity,
            middleware=() if state is None else state.middleware,
        )


async def _serve_stream_endpoint(
    endpoint: RpcStreamEndpoint,
    socket: RpcWebSocket,
    *,
    resolver: RpcResolverLike | None = None,
    context: object | Mapping[type[Any], object] | None = None,
    limits: RpcLimits | None = None,
    before_accept: RpcBeforeAccept | None = None,
    error_mapper: RpcErrorMapper | None = None,
    rejections: RpcRejections | None = None,
    activity: _Activity | None = None,
    middleware: Sequence[RpcMiddlewareLike] = (),
) -> None:
    limits = limits or RpcLimits()
    stream = endpoint.stream
    prepared = await _prepare(
        endpoint,
        socket,
        resolver,
        context,
        stream.path_model,
        before_accept,
        rejections,
    )
    if prepared is None:
        return
    connection, resolved, values, path_values = prepared
    async with _accepted(socket, connection):
        await notify_activity(activity, "connection_opened", connection)
        async with (
            _stream_scope(RpcStreamScope(endpoint, connection), middleware)
            if middleware
            else nullcontext()
        ):
            await _run_stream_endpoint(
                endpoint,
                socket,
                connection,
                resolved,
                values,
                path_values,
                limits,
                error_mapper,
                rejections,
                activity,
            )


@asynccontextmanager
async def _accepted(
    socket: RpcWebSocket,
    connection: RpcConnection,
) -> AsyncGenerator[None, None]:
    """Close accepted sockets even when expiry interrupts their initialization."""
    task = asyncio.current_task()
    assert task is not None
    cancelling = task.cancelling()
    connection._startup_task = task
    try:
        yield
    except asyncio.CancelledError as error:
        if (
            not error.args
            or error.args[0] is not _STARTUP_EXPIRED
            or task.cancelling() != cancelling + 1
        ):
            if not connection.closed and connection.close_code is None:
                connection._close_code = RpcConnectionClose.SHUTDOWN
            raise
        task.uncancel()
    except BaseException as error:
        if not connection.closed and connection.close_code is None:
            connection._close_code = (
                RpcConnectionClose.SHUTDOWN
                if isinstance(error, asyncio.CancelledError)
                else RpcConnectionClose.INTERNAL_ERROR
            )
        raise
    finally:
        connection._startup_task = None
        if not connection.closed:
            with suppress(RpcDisconnect):
                await socket.close(
                    connection.close_code or RpcConnectionClose.NORMAL,
                    connection.close_reason,
                )
            connection._mark_closed()


async def _run_stream_endpoint(
    endpoint,
    socket,
    connection,
    resolved,
    values,
    path_values,
    limits,
    error_mapper,
    rejections,
    activity,
):
    stream = endpoint.stream
    close_event = asyncio.Event()
    close_value = [RpcConnectionClose.NORMAL, ""]
    client_closed = False

    def request_close(code, reason, raw_close_code=None):
        if not close_event.is_set() or (
            not client_closed
            and close_value[0] == RpcConnectionClose.NORMAL
            and code != RpcConnectionClose.NORMAL
        ):
            close_value[:] = [code, reason]
            connection._close_code = code
            connection._close_reason = reason
            connection._raw_close_code = raw_close_code
            close_event.set()

    def client_disconnected(error: RpcDisconnect) -> None:
        nonlocal client_closed
        client_closed = True
        request_close(error.code, error.reason, error.raw_close_code)

    def close_stream_error(error: Exception) -> None:
        if isinstance(error, RpcStreamClose):
            request_close(error.close, error.reason)
            return
        _record_error(error)
        rejected = _rejection(error, rejections)
        if rejected is not None:
            request_close(REJECTION_CLOSES[rejected.rejection], rejected.reason)
            return
        try:
            mapped = (
                error
                if isinstance(error, RpcError)
                else error_mapper(error)
                if error_mapper is not None
                else None
            )
        except Exception:
            logger.exception("RPC binary stream error mapper failed")
            mapped = None
        if mapped is not None:
            request_close(
                RpcConnectionClose.POLICY_VIOLATION,
                f"{mapped.code}: {mapped.message}",
            )
            return
        logger.error("RPC binary stream failed", exc_info=error)
        request_close(RpcConnectionClose.INTERNAL_ERROR, "Internal error")

    connection._set_close_handler(request_close)
    binary_input = (
        RpcBinaryInput._create(limits.max_queue_size) if stream.has_input else None
    )
    if not stream.is_generator:
        values = {
            **values,
            RpcBinaryOutput: RpcBinaryOutput._create(socket, connection, activity),
        }
        if binary_input is not None:
            values[RpcBinaryInput] = binary_input
    generator = None
    try:
        async with (
            connection_scope(resolved, values) as scoped,
            stream.resolver_scope(scoped) as stream_resolver,
        ):
            arguments = {
                p.name: await stream_resolver.resolve(p.dependency)
                for p in stream.injected_parameters
            }
            arguments.update(path_values)

            async def write():
                try:
                    async for frame in generator:
                        if not isinstance(frame, bytes | bytearray | memoryview):
                            _record_error(
                                TypeError("Binary stream yielded a non-bytes value")
                            )
                            logger.error("Binary stream yielded a non-bytes value")
                            request_close(
                                RpcConnectionClose.INTERNAL_ERROR, "Internal error"
                            )
                            return
                        data = bytes(frame)
                        await socket.send_bytes(data)
                        await notify_activity(
                            activity,
                            "stream_frame_sent",
                            connection,
                            len(data),
                        )
                    request_close(RpcConnectionClose.NORMAL, "")
                except asyncio.CancelledError:
                    raise
                except RpcDisconnect as error:
                    client_disconnected(error)
                except Exception as error:
                    close_stream_error(error)

            async def handle():
                try:
                    await stream.function(**arguments)
                    request_close(RpcConnectionClose.NORMAL, "")
                except asyncio.CancelledError:
                    raise
                except RpcDisconnect as error:
                    client_disconnected(error)
                except Exception as error:
                    close_stream_error(error)

            async def read():
                input_ended = False
                try:
                    while True:
                        frame = await socket.receive()
                        if binary_input is None:
                            request_close(
                                RpcConnectionClose.PROTOCOL_ERROR,
                                "Binary stream is server-to-client",
                            )
                            return
                        size = len(frame.encode() if isinstance(frame, str) else frame)
                        if (
                            limits.max_message_bytes is not None
                            and size > limits.max_message_bytes
                        ):
                            request_close(RpcConnectionClose.MESSAGE_TOO_BIG, "")
                            return
                        if isinstance(frame, str):
                            if input_ended or not _is_input_end(frame):
                                request_close(
                                    RpcConnectionClose.PROTOCOL_ERROR,
                                    "Unexpected text frame on a binary stream",
                                )
                                return
                            input_ended = True
                            await binary_input._end()
                        elif input_ended:
                            request_close(
                                RpcConnectionClose.PROTOCOL_ERROR,
                                "Binary input after end",
                            )
                            return
                        else:
                            await binary_input._put(frame)
                            await notify_activity(
                                activity,
                                "stream_frame_received",
                                connection,
                                len(frame),
                            )
                except RpcDisconnect as error:
                    client_disconnected(error)
                except Exception as error:
                    close_stream_error(error)

            if stream.is_generator:
                generator = stream.function(**arguments)
                run = write()
            else:
                run = handle()
            tasks = [asyncio.create_task(run), asyncio.create_task(read())]
            try:
                connection._startup_task = None
                await close_event.wait()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if generator is not None:
                    try:
                        await generator.aclose()
                    except Exception as error:
                        close_stream_error(error)
                    generator = None
    except asyncio.CancelledError:
        shutdown = connection.close_code or RpcConnectionClose.SHUTDOWN
        close_value[:] = [shutdown, connection.close_reason]
        connection._close_code = shutdown
        raise
    except Exception as error:
        close_stream_error(error)
    finally:
        if generator is not None:
            try:
                await generator.aclose()
            except Exception as error:
                close_stream_error(error)
        if not client_closed:
            with suppress(RpcDisconnect):
                await socket.close(*close_value)
        connection._mark_closed()


def _is_input_end(frame: str) -> bool:
    try:
        RpcInputEndMessage.model_validate_json(frame)
    except ValidationError:
        return False
    return True


async def serve_socket_endpoint(
    endpoint: RpcSocketEndpoint,
    socket: RpcWebSocket,
    *,
    resolver: RpcResolverLike | None = None,
    context: object | Mapping[type[Any], object] | None = None,
    limits: RpcLimits | None = None,
    before_accept: RpcBeforeAccept | None = None,
    rejections: RpcRejections | None = None,
    middleware: Sequence[RpcMiddlewareLike] = (),
) -> None:
    async with (
        endpoint.service.drain._session(socket) as session,
        _serving_scope(endpoint, socket, middleware) as state,
    ):
        if session is None:
            _record_rejection(RpcRejection.UNAVAILABLE)
            return
        prepared = await _prepare(
            endpoint,
            socket,
            resolver,
            context,
            endpoint.socket.path_model,
            before_accept,
            rejections,
        )
        if prepared is None:
            return
        connection, resolved, values, path_values = prepared
        activity = None if state is None else state.activity
        async with _accepted(socket, connection):
            await notify_activity(activity, "connection_opened", connection)
            await _run_socket(
                endpoint,
                socket,
                connection,
                resolved,
                values,
                path_values,
                limits or RpcLimits(),
                rejections,
                activity,
            )


async def _run_socket(
    endpoint: RpcSocketEndpoint,
    socket: RpcWebSocket,
    connection: RpcConnection,
    resolved,
    values,
    path_values,
    limits: RpcLimits,
    rejections: RpcRejections | None,
    activity: _Activity | None,
) -> None:
    definition = endpoint.socket
    closes = bind_contracts(definition.closes)
    close_event = asyncio.Event()
    close_value = [RpcConnectionClose.NORMAL, ""]
    client_closed = False

    def request_close(code, reason, raw_close_code=None):
        if not close_event.is_set() or (
            not client_closed
            and close_value[0] == RpcConnectionClose.NORMAL
            and code != RpcConnectionClose.NORMAL
        ):
            close_value[:] = [code, reason]
            connection._close_code = code
            connection._close_reason = reason
            connection._raw_close_code = raw_close_code
            close_event.set()

    def client_disconnected(error: RpcDisconnect) -> None:
        nonlocal client_closed
        if not client_closed:
            client_closed = True
            request_close(error.code, error.reason, error.raw_close_code)

    def close_failure(error: Exception) -> None:
        # Concurrent forwarding usually fails inside a TaskGroup.
        leaves = _leaf_exceptions(error)
        failure = next((e for e in leaves if not isinstance(e, RpcDisconnect)), None)
        if failure is None:
            # The reader and send() already recorded a client disconnect.
            request_close(RpcConnectionClose.NORMAL, "")
            return
        _record_error(failure)
        rejected = None
        binding = contract_for(failure, closes)
        if binding is not None:
            try:
                rejected = binding.reject(failure)
            except Exception:
                logger.exception("RPC socket close binding failed")
        if rejected is None:
            rejected = _rejection(failure, rejections)
        if rejected is not None:
            request_close(REJECTION_CLOSES[rejected.rejection], rejected.reason)
            return
        logger.error("RPC socket %s failed", definition.name, exc_info=failure)
        request_close(RpcConnectionClose.INTERNAL_ERROR, "Internal error")

    connection._set_close_handler(request_close)
    socket_handle = RpcSocket._create(
        socket,
        connection,
        definition.messages,
        definition.protocol,
        definition.message_contract,
        limits,
        activity,
        request_close,
        client_disconnected,
    )
    values = {**values, RpcSocket: socket_handle}
    try:
        async with (
            connection_scope(resolved, values) as scoped,
            (
                definition.resolver_scope(scoped)
                if definition.message_parameter is None
                else nullcontext(scoped)
            ) as socket_resolver,
        ):
            arguments = (
                {
                    p.name: await socket_resolver.resolve(p.dependency)
                    for p in definition.injected_parameters
                }
                if definition.message_parameter is None
                else {}
            )
            arguments.update(path_values)

            async def respond(message):
                async with definition.resolver_scope(scoped) as message_resolver:
                    message_arguments = {
                        p.name: await message_resolver.resolve(p.dependency)
                        for p in definition.injected_parameters
                    }
                    message_arguments.update(path_values)
                    message_arguments[definition.message_parameter] = message
                    response = await definition.function(**message_arguments)
                    await socket_handle.send(response)

            async def handle():
                try:
                    if definition.message_parameter is None:
                        await definition.function(**arguments)
                    else:
                        async for frame in socket_handle:
                            await respond(frame)
                    request_close(RpcConnectionClose.NORMAL, "")
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    close_failure(error)

            async def read():
                try:
                    while True:
                        message = await socket.receive()
                        size = len(
                            message.encode() if isinstance(message, str) else message
                        )
                        if (
                            limits.max_message_bytes is not None
                            and size > limits.max_message_bytes
                        ):
                            request_close(RpcConnectionClose.MESSAGE_TOO_BIG, "")
                            return
                        message_type = (
                            RpcMessageType.TEXT
                            if isinstance(message, str)
                            else RpcMessageType.BINARY
                        )
                        if message_type not in definition.messages:
                            request_close(
                                RpcConnectionClose.PROTOCOL_ERROR,
                                f"Unsupported {message_type} message",
                            )
                            return
                        if not await socket_handle._put(message):
                            return
                        await notify_activity(
                            activity, "stream_frame_received", connection, size
                        )
                except RpcDisconnect as error:
                    client_disconnected(error)
                except Exception as error:
                    _record_error(error)
                    logger.exception("RPC socket reader failed")
                    request_close(RpcConnectionClose.INTERNAL_ERROR, "Internal error")

            tasks = [asyncio.create_task(handle()), asyncio.create_task(read())]
            try:
                connection._startup_task = None
                await close_event.wait()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        shutdown = connection.close_code or RpcConnectionClose.SHUTDOWN
        close_value[:] = [shutdown, connection.close_reason]
        connection._close_code = shutdown
        raise
    except Exception as error:
        close_failure(error)
    finally:
        if not client_closed:
            with suppress(RpcDisconnect):
                await socket.close(*close_value)
        connection._mark_closed()


def _leaf_exceptions(error: Exception) -> list[Exception]:
    if isinstance(error, ExceptionGroup):
        return [leaf for item in error.exceptions for leaf in _leaf_exceptions(item)]
    return [error]
