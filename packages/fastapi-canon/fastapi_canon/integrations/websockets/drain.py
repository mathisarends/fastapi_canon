import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from math import isfinite

from .connection import RpcConnection, RpcConnectionClose, RpcRejection, RpcWebSocket


@dataclass(eq=False)
class _DrainSession:
    task: asyncio.Task
    connection: RpcConnection | None = None
    begin: Callable[[], Awaitable[None]] | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)

    async def finish(self) -> None:
        await self.ready.wait()
        if self.finished.is_set():
            return
        if self.begin is not None:
            await self.begin()
        await self.finished.wait()

    def stop(self) -> None:
        if self.connection is not None and self.connection.close_code is None:
            self.connection._close_code = RpcConnectionClose.SERVICE_RESTART
            self.connection._close_reason = "Server restarting"
        self.task.cancel()


_session: ContextVar[_DrainSession | None] = ContextVar(
    "fastapi_canon.integrations.websockets.drain", default=None
)


def _current_drain_session() -> _DrainSession | None:
    return _session.get()


class RpcDrain:
    """One-way, shared shutdown coordinator for service connections."""

    def __init__(self) -> None:
        self._sessions: set[_DrainSession] = set()
        self._task: asyncio.Task[None] | None = None

    @property
    def draining(self) -> bool:
        return self._task is not None

    @property
    def connection_count(self) -> int:
        return len(self._sessions)

    async def drain(self, *, timeout: float = 30.0) -> None:
        """Reserve the final tenth of the timeout for cooperative cleanup."""
        if isinstance(timeout, bool) or not isfinite(timeout) or timeout < 0:
            raise ValueError("Drain timeout must be finite and nonnegative")
        if self._task is None:
            self._task = asyncio.create_task(self._drain(timeout))
            self._task.add_done_callback(_consume)
        await asyncio.shield(self._task)

    async def _drain(self, timeout: float) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        sessions = tuple(self._sessions)
        grace = [asyncio.create_task(session.finish()) for session in sessions]
        try:
            if grace:
                await asyncio.wait(grace, timeout=timeout * 0.9)
            for session in sessions:
                if not session.finished.is_set():
                    session.stop()
            for task in grace:
                task.cancel()
            work = {session.task for session in sessions} | set(grace)
            if work:
                _, pending = await asyncio.wait(
                    work, timeout=max(0, deadline - asyncio.get_running_loop().time())
                )
                for task in pending:
                    task.cancel()
                    task.add_done_callback(_consume)
                if pending:
                    logging.getLogger(__name__).warning(
                        "RPC drain deadline expired during cleanup"
                    )
        finally:
            for task in grace:
                task.cancel()
                task.add_done_callback(_consume)

    @asynccontextmanager
    async def _session(
        self, socket: RpcWebSocket
    ) -> AsyncIterator[_DrainSession | None]:
        if self.draining:
            await socket.reject(RpcRejection.UNAVAILABLE, "Server restarting")
            yield None
            return
        task = asyncio.current_task()
        assert task is not None
        session = _DrainSession(task)
        self._sessions.add(session)
        token = _session.set(session)
        try:
            yield session
        finally:
            _session.reset(token)
            self._sessions.discard(session)
            session.finished.set()
            session.ready.set()


def _consume(task: asyncio.Task) -> None:
    if not task.cancelled():
        task.exception()
