"""``WebRtcRouter``: declare session endpoints on a FastAPI router.

This module evaluates annotations eagerly: FastAPI reads the generated
endpoint signature, whose dependency refers to a closure-local factory.
"""

import asyncio
import inspect
import logging
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Annotated, Any, TypeVar, cast

from fastapi import APIRouter, Depends, HTTPException, WebSocket
from fastapi.params import Depends as DependsParam
from starlette.requests import HTTPConnection

from fastapi_canon.integrations.webrtc.declarations import (
    AdmissionErrors,
    DataChannelSpec,
    MediaPolicy,
    MessageFormat,
    Role,
    Session,
    SessionDeclaration,
    SessionLimits,
)
from fastapi_canon.integrations.webrtc.engine import Engine
from fastapi_canon.integrations.webrtc.errors import ErrorCode, close_code
from fastapi_canon.integrations.webrtc.events import (
    EventHook,
    SessionEvent,
    SessionEventKind,
)
from fastapi_canon.integrations.webrtc.ice import IceServerSource
from fastapi_canon.integrations.webrtc.runtime import (
    MINIMUM_CLEANUP_STEP_SECONDS,
    RuntimeOptions,
    SessionRuntime,
    _stop_native,
)

logger = logging.getLogger("fastapi_canon.integrations.webrtc")

Factory = TypeVar("Factory", bound=Callable[..., Any])

DECLARATION_ATTRIBUTE = "__canon_webrtc__"


class _Counter:
    def __init__(self, limit: int | None) -> None:
        self.limit = limit
        self.active = 0

    @property
    def full(self) -> bool:
        return self.limit is not None and self.active >= self.limit


class _Slots:
    """Reserved capacity of one session in its router and endpoint counters."""

    def __init__(self, *counters: _Counter) -> None:
        self._counters = counters
        self._held = True
        for counter in counters:
            counter.active += 1

    def release(self) -> None:
        if self._held:
            self._held = False
            for counter in self._counters:
                counter.active -= 1


class WebRtcRouter(APIRouter):
    """An ``APIRouter`` whose ``session`` decorator declares WebRTC sessions.

    Each session is a WebSocket signaling endpoint. Its factory is a regular
    FastAPI dependency: path and query parameters, authentication and other
    dependencies resolve before the WebSocket is accepted, so a rejected
    request allocates no peer. Dependencies, including ``yield`` dependencies,
    stay open until the session's cleanup finished.

    Including the router in an application registers its lifespan, which closes
    every active session with ``shutdown`` when the application stops.

    ``allowed_origins`` lists browser origins such as ``"https://app.example.com"``
    that may open sessions. Browsers attach cookies to cross-site WebSocket
    handshakes and CORS does not apply to them, so set it whenever session
    admission relies on cookies. A handshake from another origin is refused with
    HTTP 403 before the session factory runs. Handshakes without an ``Origin``
    header come from non-browser clients and are not affected. ``None`` (the
    default) accepts every origin.
    """

    def __init__(
        self,
        *,
        engine: Engine | None = None,
        ice_servers: IceServerSource | None = None,
        limits: SessionLimits | None = None,
        max_sessions: int | None = None,
        on_event: EventHook | None = None,
        allowed_origins: Sequence[str] | None = None,
        prefix: str = "",
        tags: list[str] | None = None,
        dependencies: Sequence[DependsParam] | None = None,
    ) -> None:
        super().__init__(
            prefix=prefix,
            tags=list(tags or []),
            dependencies=list(dependencies or []),
            lifespan=self._lifespan,
        )
        if max_sessions is not None and max_sessions <= 0:
            raise ValueError("max_sessions must be positive")
        self.engine = engine
        self.ice_servers = ice_servers
        self.limits = limits or SessionLimits()
        self.event_hook = on_event
        self.allowed_origins = _origins(allowed_origins)
        self.declarations: list[SessionDeclaration] = []
        self._counter = _Counter(max_sessions)
        self._runtimes: set[SessionRuntime] = set()
        self._closing = False

    @property
    def active_sessions(self) -> int:
        return len(self._runtimes)

    def session(
        self,
        path: str,
        *,
        name: str | None = None,
        role: Role = Role.ANSWERER,
        media: MediaPolicy | None = None,
        data_channels: Sequence[DataChannelSpec] = (),
        limits: SessionLimits | None = None,
        max_sessions: int | None = None,
        dependencies: Sequence[DependsParam] | None = None,
        admission_errors: AdmissionErrors | None = None,
    ) -> Callable[[Factory], Factory]:
        """Declare a session endpoint whose factory returns a :class:`Session`.

        The factory may be a function, coroutine function or generator
        dependency. ``name`` defaults to the factory name and becomes the
        generated client's method name.
        """

        def decorator(factory: Factory) -> Factory:
            declaration = SessionDeclaration(
                name=name or factory.__name__,
                path=self.prefix + path,
                role=role,
                media=media or MediaPolicy(),
                channels=tuple(data_channels),
                limits=limits or self.limits,
                max_sessions=max_sessions,
                description=inspect.getdoc(factory),
                admission_errors=admission_errors,
            )
            if any(d.name == declaration.name for d in self.declarations):
                raise ValueError(f"Session {declaration.name!r} is already declared")
            self.declarations.append(declaration)
            counter = _Counter(max_sessions)

            async def endpoint(
                websocket: WebSocket,
                session: Annotated[Session, Depends(factory)],
            ) -> None:
                if not isinstance(cast(object, session), Session):
                    logger.error(
                        "Session factory %s returned %s instead of Session",
                        declaration.name,
                        type(session).__name__,
                    )
                    await _refuse(websocket, ErrorCode.HANDLER_FAILURE)
                    return
                await self._serve(declaration, counter, websocket, session)

            endpoint.__doc__ = factory.__doc__
            endpoint.__name__ = f"webrtc_session_{declaration.name}"
            setattr(endpoint, DECLARATION_ATTRIBUTE, declaration)
            self.add_api_websocket_route(
                path,
                endpoint,
                name=declaration.name,
                dependencies=[Depends(self._check_origin), *(dependencies or ())],
            )
            return factory

        return decorator

    async def _serve(
        self,
        declaration: SessionDeclaration,
        counter: _Counter,
        websocket: WebSocket,
        session: Session,
    ) -> None:
        runtime: SessionRuntime | None = None
        try:
            if session.engine is None and self.engine is None:
                logger.error("Session %s has no engine binding", declaration.name)
                await _refuse(websocket, ErrorCode.HANDLER_FAILURE)
                return
            unknown = _invalid_handlers(declaration, session)
            if unknown:
                logger.error(
                    "Session %s has handlers for undeclared or send-only channels: %s",
                    declaration.name,
                    ", ".join(sorted(unknown)),
                )
                await _refuse(websocket, ErrorCode.HANDLER_FAILURE)
                return
            runtime = self._admit(declaration, counter, websocket, session)
            if runtime is None:
                code = ErrorCode.SHUTDOWN if self._closing else ErrorCode.SESSION_LIMIT
                await _refuse(websocket, code)
                return
            self._runtimes.add(runtime)
            await runtime.serve()
        finally:
            if runtime is None:
                # A returned Session transfers its tracks even if admission fails.
                for track in {id(t): t for t in session.tracks}.values():
                    _stop_native(track)
            else:
                self._runtimes.discard(runtime)

    def _check_origin(self, connection: HTTPConnection) -> None:
        """Refuse cross-site handshakes before the session factory runs."""
        allowed = self.allowed_origins
        origin = connection.headers.get("origin")
        if allowed is None or origin is None:
            return
        if origin.lower().rstrip("/") not in allowed:
            logger.warning("Refused WebRTC session from origin %r", origin)
            raise HTTPException(status_code=403, detail="Origin not allowed")

    def _admit(
        self,
        declaration: SessionDeclaration,
        counter: _Counter,
        websocket: WebSocket,
        session: Session,
    ) -> SessionRuntime | None:
        session_id = uuid.uuid4().hex
        if self._closing:
            return None
        if self._counter.full or counter.full:
            self._emit(
                SessionEvent(
                    kind=SessionEventKind.REJECTED,
                    name=declaration.name,
                    session_id=session_id,
                    elapsed_seconds=0.0,
                    code=ErrorCode.SESSION_LIMIT,
                )
            )
            return None
        return SessionRuntime(
            declaration=declaration,
            options=RuntimeOptions(
                engine=cast(
                    Engine,
                    session.engine if session.engine is not None else self.engine,
                ),
                ice_servers=self.ice_servers,
                on_event=self.event_hook,
            ),
            websocket=websocket,
            session=session,
            session_id=session_id,
            slots=_Slots(self._counter, counter),
        )

    def _emit(self, event: SessionEvent) -> None:
        if self.event_hook is None:
            return
        try:
            self.event_hook(event)
        except Exception:
            logger.exception("Session event hook failed")

    async def aclose(self, timeout: float | None = None) -> None:
        """Close every active session with ``shutdown`` and wait for cleanup.

        New sessions are refused afterwards. ``timeout`` defaults to the largest
        configured shutdown deadline plus the minimum budgets of ``on_close`` and
        engine cleanup.
        """
        self._closing = True
        runtimes = list(self._runtimes)
        for runtime in runtimes:
            runtime.terminate(ErrorCode.SHUTDOWN)
        if not runtimes:
            return
        if timeout is None:
            timeout = 2 * MINIMUM_CLEANUP_STEP_SECONDS + max(
                runtime.declaration.limits.shutdown_timeout_seconds
                for runtime in runtimes
            )
        waiters = [asyncio.ensure_future(r.wait_finished()) for r in runtimes]
        _, pending = await asyncio.wait(waiters, timeout=timeout)
        for waiter in pending:
            waiter.cancel()
        if pending:
            logger.warning("%d WebRTC session(s) did not finish cleanup", len(pending))

    @asynccontextmanager
    async def _lifespan(self, _app: Any) -> AsyncIterator[None]:
        self._closing = False
        try:
            yield
        finally:
            await self.aclose()


def _origins(origins: Sequence[str] | None) -> frozenset[str] | None:
    if origins is None:
        return None
    if isinstance(origins, str):
        raise TypeError("allowed_origins must be a sequence of origins, not a str")
    normalized = set()
    for origin in origins:
        value = origin.strip().lower().rstrip("/")
        scheme, separator, host = value.partition("://")
        if not separator or not scheme or not host or "/" in host:
            raise ValueError(
                f"Invalid origin {origin!r}; use scheme://host[:port], for "
                "example 'https://app.example.com'"
            )
        normalized.add(value)
    return frozenset(normalized)


def _invalid_handlers(declaration: SessionDeclaration, session: Session) -> set[str]:
    """Handler keys that name no receiving channel or use a different spec of the
    same name."""
    declared = {
        spec.name: spec
        for spec in declaration.channels
        if spec.receive_format != MessageFormat.NONE
    }
    unknown = set(session.message_handlers) - declared.keys()
    for key in session.on_message:
        if isinstance(key, DataChannelSpec) and declared.get(key.name) != key:
            unknown.add(key.name)
    return unknown


async def _refuse(websocket: WebSocket, code: ErrorCode) -> None:
    """Accept only to deliver a categorized close code to the browser."""
    await websocket.accept(subprotocol=_subprotocol(websocket))
    await websocket.close(code=close_code(code), reason=code.value)


def _subprotocol(websocket: WebSocket) -> str | None:
    from fastapi_canon.integrations.webrtc.protocol import SUBPROTOCOL

    offered = websocket.scope.get("subprotocols") or []
    return SUBPROTOCOL if SUBPROTOCOL in offered else None
