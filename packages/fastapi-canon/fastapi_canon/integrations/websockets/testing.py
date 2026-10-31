import asyncio
import json
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

from fastapi_canon.integrations.websockets.connection import (
    RpcConnectionClose,
    RpcDisconnect,
    RpcHandshake,
    RpcLimits,
    RpcRejection,
    RpcRejections,
)
from fastapi_canon.integrations.websockets.dependencies import RpcResolverLike
from fastapi_canon.integrations.websockets.server import RpcErrorMapper
from fastapi_canon.integrations.websockets.service import (
    RpcService,
    RpcSocketEndpoint,
    RpcStreamEndpoint,
)
from fastapi_canon.integrations.websockets.streams import RpcInputEndMessage
from fastapi_canon.integrations.websockets.websocket import (
    CLOSE_CODES,
    REJECTION_CLOSE_CODES,
)

_CLOSED = object()

__all__ = [
    "InMemorySocket",
    "InMemoryClientSocket",
    "InMemorySocketFactory",
    "RpcTestClient",
    "RpcTestConnectionClosed",
    "RpcTestError",
    "RpcTestStream",
    "RpcTestHandshakeRejected",
    "SocketFaults",
]


class SocketFaults:
    """Directional faults for one in-memory socket; gates create no worker tasks."""

    def __init__(
        self,
        *,
        drop_after: int | None = None,
        drop_when: Callable[[str | bytes], bool] | None = None,
        close_code: RpcConnectionClose | int = 1006,
        reason: str = "",
        delay: float | Callable[[str | bytes], float] = 0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        half_open: bool = False,
    ) -> None:
        if drop_after is not None and drop_after < 0:
            raise ValueError("drop_after must be non-negative or None")
        if not callable(delay) and delay < 0:
            raise ValueError("delay must be non-negative")
        self.drop_after = drop_after
        self.drop_when = drop_when
        self.close_code = close_code
        self.reason = reason
        self.delay = delay
        self.sleep = sleep
        self.half_open = half_open
        self.frames_sent = 0
        self.blocked = asyncio.Event()
        self.held = asyncio.Event()
        self._send_gate = asyncio.Event()
        self._send_gate.set()
        self._closed = asyncio.Event()
        self._holding = False
        self._held: deque[tuple[asyncio.Queue[Any], str | bytes]] = deque()
        self._bound = False

    @property
    def held_count(self) -> int:
        return len(self._held)

    def hold(self) -> None:
        """Buffer delivered frames while allowing send to finish."""
        self._holding = True
        self.held.clear()

    def release(self) -> None:
        self._holding = False
        self.held.clear()
        while self._held:
            queue, frame = self._held.popleft()
            if not self._closed.is_set():
                queue.put_nowait(frame)

    def block(self) -> None:
        """Block send itself until unblock or disconnect."""
        self.blocked.clear()
        self._send_gate.clear()

    def unblock(self) -> None:
        self._send_gate.set()

    def _bind(self) -> None:
        if self._bound:
            raise ValueError("Use separate SocketFaults instances per socket direction")
        self._bound = True

    def _close(self) -> None:
        self._held.clear()
        self._closed.set()
        self._send_gate.set()

    async def _wait(self, operation: Awaitable[None], socket: "InMemorySocket") -> None:
        waiting = asyncio.ensure_future(operation)
        closing = asyncio.create_task(self._closed.wait())
        try:
            await asyncio.wait((waiting, closing), return_when=asyncio.FIRST_COMPLETED)
            socket._check_open()
            await waiting
        finally:
            for task in (waiting, closing):
                if not task.done():
                    task.cancel()
            await asyncio.gather(waiting, closing, return_exceptions=True)

    async def _send(
        self, socket: "InMemorySocket", queue: asyncio.Queue[Any], frame: str | bytes
    ) -> None:
        socket._check_open()
        if not self._send_gate.is_set():
            self.blocked.set()
            await self._wait(self._send_gate.wait(), socket)
        if self.drop_after == 0 or (
            self.drop_when is not None and self.drop_when(frame)
        ):
            socket._disconnect(RpcDisconnect(self.close_code, self.reason))
            socket._check_open()
        duration = self.delay(frame) if callable(self.delay) else self.delay
        if duration < 0:
            raise ValueError("Frame delay must be non-negative")
        if duration:
            await self._wait(self.sleep(duration), socket)
        self.frames_sent += 1
        if not self.half_open:
            if self._holding:
                self._held.append((queue, frame))
                self.held.set()
            else:
                queue.put_nowait(frame)
        if self.drop_after is not None and self.frames_sent >= self.drop_after:
            socket._disconnect(RpcDisconnect(self.close_code, self.reason))


class InMemorySocket:
    def __init__(
        self,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        query_params: Mapping[str, str] | None = None,
        subprotocols: Iterable[str] = (),
        client_to_server: SocketFaults | None = None,
        server_to_client: SocketFaults | None = None,
    ) -> None:
        self._handshake = RpcHandshake(
            path=path,
            headers=headers or {},
            query_params=query_params or {},
            subprotocols=tuple(subprotocols),
        )
        self._incoming: asyncio.Queue[Any] = asyncio.Queue()
        self._outgoing: asyncio.Queue[Any] = asyncio.Queue()
        self.accepted = False
        self.subprotocol: str | None = None
        self.rejection: tuple[RpcRejection, str] | None = None
        self.closed: tuple[RpcConnectionClose, str] | None = None
        self.disconnect_error: RpcDisconnect | None = None
        self.client_to_server = client_to_server or SocketFaults()
        self.server_to_client = server_to_client or SocketFaults()
        self.client_to_server._bind()
        self.server_to_client._bind()
        self._handshake_complete = asyncio.Event()
        self.client = InMemoryClientSocket(self)

    @property
    def handshake(self) -> RpcHandshake:
        return self._handshake

    async def accept(self, subprotocol: str | None = None) -> None:
        self._check_open()
        self.accepted = True
        self.subprotocol = subprotocol
        self._handshake_complete.set()

    async def reject(
        self,
        rejection: RpcRejection,
        reason: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.rejection = (rejection, reason)
        self._disconnect(RpcDisconnect(REJECTION_CLOSE_CODES[rejection], reason))

    async def receive(self) -> str | bytes:
        return await self._receive(self._incoming)

    async def send(self, message: str) -> None:
        await self.server_to_client._send(self, self._outgoing, message)

    async def send_bytes(self, data: bytes) -> None:
        await self.server_to_client._send(self, self._outgoing, data)

    async def close(self, close: RpcConnectionClose, reason: str) -> None:
        if self.closed is None:
            self.closed = (close, reason)
        self._disconnect(RpcDisconnect(close, reason))

    async def client_send(self, message: str | bytes) -> None:
        await self.client_to_server._send(self, self._incoming, message)

    async def client_receive(self) -> str | bytes:
        return await self._receive(self._outgoing)

    async def client_disconnect(
        self,
        code: RpcConnectionClose | int = RpcConnectionClose.NORMAL,
        reason: str = "",
    ) -> None:
        self._disconnect(RpcDisconnect(code, reason))

    def _check_open(self) -> None:
        if self.disconnect_error is not None:
            error = self.disconnect_error
            raise RpcDisconnect(
                error.code if error.raw_close_code is None else error.raw_close_code,
                error.reason,
            )

    def _disconnect(self, error: RpcDisconnect) -> None:
        if self.disconnect_error is not None:
            return
        self.disconnect_error = error
        self.client_to_server._close()
        self.server_to_client._close()
        self._incoming.put_nowait(error)
        self._outgoing.put_nowait(error)
        self._handshake_complete.set()

    async def _receive(self, queue: asyncio.Queue[Any]) -> str | bytes:
        if queue.empty():
            self._check_open()
        value = await queue.get()
        if isinstance(value, RpcDisconnect):
            self._check_open()
        return value


class InMemoryClientSocket:
    """The generated client's send/recv/close view of the same socket queues."""

    def __init__(self, socket: InMemorySocket) -> None:
        self.server_socket = socket

    @property
    def close_code(self) -> int | None:
        error = self.server_socket.disconnect_error
        if error is None:
            return None
        return error.raw_close_code or CLOSE_CODES[error.code]

    @property
    def close_reason(self) -> str:
        error = self.server_socket.disconnect_error
        return "" if error is None else error.reason

    async def send(self, message: str | bytes) -> None:
        await self.server_socket.client_send(message)

    async def recv(self) -> str | bytes:
        return await self.server_socket.client_receive()

    async def close(self) -> None:
        await self.server_socket.client_disconnect()


class RpcTestHandshakeRejected(Exception):
    def __init__(
        self,
        *,
        status: int | None = None,
        close_code: int | None = None,
        reason: str = "",
    ) -> None:
        self.status_code = status
        self.close_code = close_code
        self.reason = reason
        super().__init__(reason or "The in-memory handshake was rejected")


class InMemorySocketFactory:
    """Repeated generated-client connections to a service without network I/O."""

    def __init__(
        self,
        service: RpcService,
        *,
        configure: Callable[[InMemorySocket, int], None] | None = None,
        resolver: RpcResolverLike | None = None,
        context: object | Mapping[type[Any], object] | None = None,
        limits: RpcLimits | None = None,
    ) -> None:
        self.service = service
        self.configure = configure
        self.resolver = resolver
        self.context = context
        self.limits = limits
        self.attempts = 0
        self.sockets: list[InMemorySocket] = []
        self._tasks: list[asyncio.Task[None]] = []
        self._rejections: deque[RpcTestHandshakeRejected] = deque()
        self._closed = False

    def reject_next(
        self,
        *,
        status: int | None = None,
        close_code: int | None = None,
        reason: str = "",
    ) -> None:
        if status is None and close_code is None:
            raise ValueError("Provide a handshake status or close_code")
        self._rejections.append(
            RpcTestHandshakeRejected(
                status=status, close_code=close_code, reason=reason
            )
        )

    async def __call__(
        self,
        url: str,
        *,
        subprotocols: list[str] | None = None,
        additional_headers: Mapping[str, str] | None = None,
    ) -> InMemoryClientSocket:
        if self._closed:
            raise RuntimeError("The in-memory socket factory is closed")
        self.attempts += 1
        if self._rejections:
            raise self._rejections.popleft()
        socket = InMemorySocket(
            unquote(urlsplit(url).path) or "/",
            headers=additional_headers,
            subprotocols=subprotocols or (),
        )
        if self.configure is not None:
            self.configure(socket, self.attempts)
        self.sockets.append(socket)
        serving = asyncio.create_task(
            self.service.serve(
                socket, resolver=self.resolver, context=self.context, limits=self.limits
            )
        )
        self._tasks.append(serving)
        handshake = asyncio.create_task(socket._handshake_complete.wait())
        try:
            await asyncio.wait(
                (serving, handshake), return_when=asyncio.FIRST_COMPLETED
            )
            if serving.done():
                serving.result()
            if socket.rejection is not None:
                raise RpcTestHandshakeRejected(
                    close_code=socket.client.close_code, reason=socket.rejection[1]
                )
            socket._check_open()
            return socket.client
        except BaseException:
            await socket.client_disconnect()
            serving.cancel()
            await asyncio.gather(serving, return_exceptions=True)
            raise
        finally:
            handshake.cancel()
            await asyncio.gather(handshake, return_exceptions=True)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for socket in self.sockets:
            await socket.client_disconnect()
        outcomes = await asyncio.gather(*self._tasks, return_exceptions=True)
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                raise outcome

    async def __aenter__(self) -> "InMemorySocketFactory":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


@dataclass(frozen=True, slots=True)
class RpcTestError(Exception):
    rpc_code: int
    code: str
    message: str
    details: Any = None


class RpcTestConnectionClosed(Exception):
    pass


class RpcTestClient:
    def __init__(
        self,
        service: RpcService,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        subprotocols: Iterable[str] = (),
        resolver: RpcResolverLike | None = None,
        context: object | Mapping[type[Any], object] | None = None,
        error_mapper: RpcErrorMapper | None = None,
        limits: RpcLimits | None = None,
        rejections: RpcRejections | None = None,
    ) -> None:
        self.service = service
        self.socket = InMemorySocket(path, headers=headers, subprotocols=subprotocols)
        self.resolver = resolver
        self.context = context
        self.error_mapper = error_mapper
        self.limits = limits
        self.rejections = rejections
        self._task: asyncio.Task | None = None
        self._reader: asyncio.Task | None = None
        self._id = 0
        self._notifications: asyncio.Queue[object] = asyncio.Queue()
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        matched = service.match(path)
        endpoint = None if matched is None else matched[0]
        self._socket = isinstance(endpoint, RpcSocketEndpoint)
        self._stream = self._socket or isinstance(endpoint, RpcStreamEndpoint)
        self._input = isinstance(endpoint, RpcStreamEndpoint) and (
            endpoint.stream.has_input
        )

    async def __aenter__(self):
        self._task = asyncio.create_task(
            self.service.serve(
                self.socket,
                resolver=self.resolver,
                context=self.context,
                error_mapper=self.error_mapper,
                limits=self.limits,
                rejections=self.rejections,
            )
        )
        if not self._stream:
            self._reader = asyncio.create_task(self._read())
        await asyncio.sleep(0)
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.socket.client_disconnect()
        if self._task is not None:
            with suppress(asyncio.CancelledError):
                await self._task
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)

    async def request(
        self, method: str, params: Mapping[str, Any] | None = None
    ) -> Any:
        if self._stream:
            raise TypeError("request() is unavailable for stream and socket endpoints")
        if self._reader is not None and self._reader.done():
            raise self._connection_closed()
        self._id += 1
        request_id = self._id
        response = asyncio.get_running_loop().create_future()
        self._pending[request_id] = response
        try:
            await self.socket.client_send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": method,
                        "params": dict(params or {}),
                    }
                )
            )
            value = await response
        finally:
            self._pending.pop(request_id, None)
        if "error" in value:
            item = value["error"]
            data = item.get("data", {})
            raise RpcTestError(
                item["code"],
                data.get("code", ""),
                item["message"],
                data.get("details"),
            )
        return value["result"]

    async def notify(
        self, method: str, params: Mapping[str, Any] | None = None
    ) -> None:
        await self.socket.client_send(
            json.dumps(
                {"jsonrpc": "2.0", "method": method, "params": dict(params or {})}
            )
        )

    async def next_notification(
        self, *, timeout: float | None = None
    ) -> tuple[str, Any]:
        async with asyncio.timeout(timeout):
            item = await self._notifications.get()
        if item is _CLOSED:
            self._notifications.put_nowait(_CLOSED)
            raise self._connection_closed()
        return item  # type: ignore[return-value]

    async def _read(self) -> None:
        try:
            while True:
                value = json.loads(await self.socket.client_receive())
                if not isinstance(value, dict):
                    continue
                if "method" in value:
                    await self._notifications.put((value["method"], value["params"]))
                else:
                    response = self._pending.get(value.get("id"))
                    if response is not None and not response.done():
                        response.set_result(value)
        except RpcDisconnect:
            error = self._connection_closed()
            for response in self._pending.values():
                if not response.done():
                    response.set_exception(error)
            await self._notifications.put(_CLOSED)

    def _connection_closed(self) -> "RpcTestConnectionClosed":
        return RpcTestConnectionClosed(self.socket.rejection or self.socket.closed)

    async def next_message(self) -> str | bytes:
        """Receive the next text or binary message of a socket endpoint."""
        if not self._socket:
            raise TypeError("next_message() is only available for socket endpoints")
        try:
            return await self.socket.client_receive()
        except RpcDisconnect as error:
            raise RpcTestConnectionClosed(
                self.socket.rejection or self.socket.closed
            ) from error

    async def send_message(self, message: str | bytes) -> None:
        """Send a text (``str``) or binary message to a socket endpoint."""
        if not self._socket:
            raise TypeError("send_message() is only available for socket endpoints")
        await self.socket.client_send(message)

    async def next_frame(self) -> bytes:
        if not self._stream or self._socket:
            raise TypeError("next_frame() is only available for stream endpoints")
        try:
            value = await self.socket.client_receive()
        except RpcDisconnect as error:
            raise RpcTestConnectionClosed(self.socket.closed) from error
        if not isinstance(value, bytes):
            raise TypeError("Expected a binary frame")
        return value

    async def send_frame(self, data: bytes | bytearray | memoryview) -> None:
        if not self._input:
            raise TypeError("send_frame() is only available for streams with input")
        await self.socket.client_send(bytes(data))

    async def end_input(self) -> None:
        if not self._input:
            raise TypeError("end_input() is only available for streams with input")
        await self.socket.client_send(RpcInputEndMessage(type="end").model_dump_json())

    async def closed(self) -> tuple[RpcConnectionClose, str] | None:
        """Wait until the server finished the connection and return its close."""
        if self._task is not None:
            with suppress(asyncio.CancelledError):
                await self._task
        return self.socket.closed


class RpcTestStream(RpcTestClient):
    def __init__(self, service: RpcService, path: str, **kwargs: Any) -> None:
        super().__init__(service, path, **kwargs)
        if not self._stream:
            raise TypeError(f"{path!r} is not a binary stream endpoint")
