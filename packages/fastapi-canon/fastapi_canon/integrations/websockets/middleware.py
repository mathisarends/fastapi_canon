import asyncio
import logging
import time
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from fastapi_canon.integrations.websockets._activity import _Activity, _activity_for
from fastapi_canon.integrations.websockets.connection import (
    RpcConnection,
    RpcConnectionClose,
    RpcDisconnect,
    RpcHandshake,
    RpcRejection,
    RpcWebSocket,
)
from fastapi_canon.integrations.websockets.constants import LOGGER_NAME
from fastapi_canon.integrations.websockets.envelopes import RpcRequestId
from fastapi_canon.integrations.websockets.protocol import (
    RpcMethodDefinition,
    RpcStreamDefinition,
)

if TYPE_CHECKING:
    from fastapi_canon.integrations.websockets.service import RpcAnyEndpoint

logger: logging.Logger = logging.getLogger(LOGGER_NAME)


class RpcEndpointInfo(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def path(self) -> str: ...


class RpcStreamEndpointInfo(RpcEndpointInfo, Protocol):
    @property
    def stream(self) -> RpcStreamDefinition: ...


@dataclass(frozen=True, slots=True)
class RpcRequestContext:
    raw_request: object
    method: str | None
    request_id: RpcRequestId
    notification: bool
    connection: RpcConnection | None = None
    metadata: Mapping[str, str] | None = None


@dataclass(frozen=True, slots=True)
class RpcConnectionOutcome:
    accepted: bool
    rejection: RpcRejection | None
    close_code: RpcConnectionClose | None
    raw_close_code: int | None
    duration: float
    error: BaseException | None = None
    cancelled: bool = False
    accepted_duration: float | None = None


@dataclass(frozen=True, slots=True)
class RpcRequestOutcome:
    response: object | None
    duration: float
    error: BaseException | None = None
    cancelled: bool = False
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class RpcStreamOutcome:
    close_code: RpcConnectionClose | None
    raw_close_code: int | None
    duration: float
    error: BaseException | None = None
    cancelled: bool = False


@dataclass(slots=True)
class RpcConnectionScope:
    endpoint: RpcEndpointInfo
    handshake: RpcHandshake
    connection: RpcConnection | None = None
    outcome: RpcConnectionOutcome | None = None


@dataclass(slots=True)
class RpcRequestScope:
    request: RpcRequestContext
    endpoint: RpcEndpointInfo | None = None
    method: RpcMethodDefinition | None = None
    outcome: RpcRequestOutcome | None = None

    @property
    def metadata(self) -> Mapping[str, str] | None:
        return self.request.metadata


@dataclass(slots=True)
class RpcStreamScope:
    endpoint: RpcStreamEndpointInfo
    connection: RpcConnection
    outcome: RpcStreamOutcome | None = None


class RpcMiddlewareLike(Protocol):
    def connection(
        self, scope: RpcConnectionScope
    ) -> AbstractAsyncContextManager[None]: ...

    def request(self, scope: RpcRequestScope) -> AbstractAsyncContextManager[None]: ...

    def stream(self, scope: RpcStreamScope) -> AbstractAsyncContextManager[None]: ...


class RpcMiddleware:
    """Override lifecycle scopes and activity callbacks to instrument operations."""

    def connection(
        self, scope: RpcConnectionScope
    ) -> AbstractAsyncContextManager[None]:
        return nullcontext()

    def request(self, scope: RpcRequestScope) -> AbstractAsyncContextManager[None]:
        return nullcontext()

    def stream(self, scope: RpcStreamScope) -> AbstractAsyncContextManager[None]:
        return nullcontext()

    async def connection_opened(self, connection: RpcConnection) -> None:
        pass

    async def notification_sent(self, name: str, size: int) -> None:
        pass

    async def slow_consumer_closed(self, connection: RpcConnection) -> None:
        pass

    async def stream_frame_sent(self, connection: RpcConnection, size: int) -> None:
        pass

    async def stream_frame_received(self, connection: RpcConnection, size: int) -> None:
        pass


@asynccontextmanager
async def _middleware_scope(
    middleware: Sequence[RpcMiddlewareLike],
    kind: str,
    scope: RpcConnectionScope | RpcRequestScope | RpcStreamScope,
    *,
    on_error: Callable[[BaseException], None] | None = None,
) -> AsyncGenerator[None, None]:
    entered: list[AbstractAsyncContextManager[None]] = []
    error: BaseException | None = None
    started = time.perf_counter()
    try:
        for item in middleware:
            try:
                manager = getattr(item, kind)(scope)
                await manager.__aenter__()
            except Exception:
                logger.exception("RPC middleware %s enter failed", kind)
            else:
                entered.append(manager)
        yield
    except BaseException as caught:
        error = caught
        if on_error is not None:
            on_error(caught)
        raise
    finally:
        if scope.outcome is None:
            cancelled = isinstance(error, asyncio.CancelledError)
            duration = time.perf_counter() - started
            if isinstance(scope, RpcRequestScope):
                scope.outcome = RpcRequestOutcome(None, duration, error, cancelled)
            elif isinstance(scope, RpcStreamScope):
                scope.outcome = RpcStreamOutcome(
                    scope.connection.close_code,
                    scope.connection.raw_close_code,
                    duration,
                    error,
                    cancelled,
                )
            else:
                scope.outcome = RpcConnectionOutcome(
                    False,
                    None,
                    None,
                    None,
                    duration,
                    error,
                    cancelled,
                )
        interrupted: BaseException | None = None
        for manager in reversed(entered):
            try:
                # A middleware cannot suppress a serving failure.
                await manager.__aexit__(
                    None if error is None else type(error),
                    error,
                    None if error is None else error.__traceback__,
                )
            except Exception:
                logger.exception("RPC middleware %s exit failed", kind)
            except BaseException as caught:
                interrupted = caught
                if error is None and on_error is not None:
                    on_error(caught)
        if interrupted is not None and error is None:
            raise interrupted


@dataclass(slots=True)
class _ServingState:
    scope: RpcConnectionScope
    socket: RpcWebSocket
    middleware: tuple[RpcMiddlewareLike, ...]
    activity: _Activity | None
    started_at: float = field(default_factory=time.perf_counter)
    error: BaseException | None = None
    disconnect_error: BaseException | None = None
    rejection: RpcRejection | None = None
    accepted_at: float | None = None
    cancelled: bool = False

    def finish(self) -> None:
        connection = self.scope.connection
        finished = time.perf_counter()
        self.scope.outcome = RpcConnectionOutcome(
            accepted=self.accepted_at is not None,
            rejection=self.rejection,
            close_code=None if connection is None else connection.close_code,
            raw_close_code=None if connection is None else connection.raw_close_code,
            duration=finished - self.started_at,
            error=self.error,
            cancelled=self.cancelled,
            accepted_duration=None
            if self.accepted_at is None
            else finished - self.accepted_at,
        )


_serving: ContextVar[_ServingState | None] = ContextVar(
    "fastapi_canon.integrations.websockets.serving", default=None
)


def _current_serving() -> _ServingState | None:
    return _serving.get()


def _record_error(error: BaseException) -> None:
    state = _serving.get()
    if state is not None and isinstance(error, asyncio.CancelledError):
        state.cancelled = True
    if (
        state is not None
        and error is not state.disconnect_error
        and (state.error is None or isinstance(state.error, asyncio.CancelledError))
        and not isinstance(error, RpcDisconnect)
    ):
        state.error = error


def _record_rejection(rejection: RpcRejection) -> None:
    state = _serving.get()
    if state is not None:
        state.rejection = rejection


@asynccontextmanager
async def _serving_scope(
    endpoint: "RpcAnyEndpoint",
    socket: RpcWebSocket,
    middleware: Sequence[RpcMiddlewareLike] = (),
) -> AsyncGenerator[_ServingState | None, None]:
    active = _serving.get()
    if (
        active is not None
        and active.socket is socket
        and active.scope.endpoint is endpoint
    ):
        additional = tuple(
            item
            for item in middleware
            if not any(item is configured for configured in active.middleware)
        )
        if not additional:
            yield active
            return
        previous = active.middleware, active.activity
        active.middleware = (*active.middleware, *additional)
        active.activity = _activity_for(active.middleware)
        try:
            async with _connection_middleware(active, additional):
                yield active
        finally:
            active.middleware, active.activity = previous
        return
    chain = (*middleware, *endpoint.middleware)
    if not chain:
        # A nested uninstrumented connection must not update its caller's scope.
        token = _serving.set(None) if active is not None else None
        try:
            yield None
        finally:
            if token is not None:
                _serving.reset(token)
        return
    state = _ServingState(
        RpcConnectionScope(endpoint, socket.handshake),
        socket,
        chain,
        _activity_for(chain),
    )
    token = _serving.set(state)
    try:
        async with _connection_middleware(state, chain):
            yield state
    finally:
        _serving.reset(token)


@asynccontextmanager
async def _connection_middleware(
    state: _ServingState, middleware: Sequence[RpcMiddlewareLike]
) -> AsyncGenerator[None, None]:
    async with _middleware_scope(middleware, "connection", state.scope):
        try:
            yield
        except BaseException as error:
            _record_error(error)
            raise
        finally:
            state.finish()


@asynccontextmanager
async def _stream_scope(
    scope: RpcStreamScope, middleware: Sequence[RpcMiddlewareLike]
) -> AsyncGenerator[None, None]:
    started = time.perf_counter()
    async with _middleware_scope(middleware, "stream", scope):
        try:
            yield
        except BaseException as error:
            _record_error(error)
            raise
        finally:
            state = _serving.get()
            error = None if state is None else state.error
            scope.outcome = RpcStreamOutcome(
                close_code=scope.connection.close_code,
                raw_close_code=scope.connection.raw_close_code,
                duration=time.perf_counter() - started,
                error=error,
                cancelled=state is not None and state.cancelled,
            )
