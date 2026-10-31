import asyncio
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Generic, Self, cast

from pydantic import ValidationError
from pydantic_core import PydanticSerializationError
from typing_extensions import TypeVar

from fastapi_canon.integrations.websockets._activity import _Activity, notify_activity
from fastapi_canon.integrations.websockets._socket_messages import SocketMessages
from fastapi_canon.integrations.websockets.connection import (
    RpcConnection,
    RpcConnectionClose,
    RpcDisconnect,
    RpcLimits,
    RpcWebSocket,
)


class RpcMessageType(StrEnum):
    TEXT = "text"
    BINARY = "binary"


type RpcSocketMessage = str | bytes

_IncomingT = TypeVar("_IncomingT", default=RpcSocketMessage)
_OutgoingT = TypeVar("_OutgoingT", default=str | bytes | bytearray | memoryview)

_TOKEN = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*")


@dataclass(frozen=True, slots=True)
class RpcSocketProtocol:
    """The application protocol a socket carries, declared for its contract.

    This declaration alone does not decode, validate, or dispatch messages. It
    identifies the protocol so generators can select a client binding. Handler
    annotations independently enable JSON validation.
    ``schemas`` lists URI references to the protocol's specification files;
    relative references resolve against the contract document's location.
    """

    name: str
    version: str | None = None
    encoding: str = "json"
    schemas: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _TOKEN.fullmatch(self.name):
            raise ValueError(
                "Socket protocol names are lowercase tokens such as 'cdp' or "
                "'graphql-ws'"
            )
        if self.version is not None and (
            not isinstance(self.version, str)
            or not self.version
            or self.version != self.version.strip()
        ):
            raise ValueError("Socket protocol versions are non-empty strings")
        if not isinstance(self.encoding, str) or not _TOKEN.fullmatch(self.encoding):
            raise ValueError(
                "Socket protocol encodings are lowercase tokens such as 'json'"
            )
        schemas: Iterable[object] = self.schemas
        if isinstance(schemas, str) or not isinstance(schemas, Iterable):
            raise TypeError("Socket protocol schemas must be a sequence of strings")
        values = tuple(schemas)
        if any(
            not isinstance(value, str) or not value or value.split() != [value]
            for value in values
        ):
            raise ValueError(
                "Socket protocol schemas must be URI references without whitespace"
            )
        if len(set(values)) != len(values):
            raise ValueError("Socket protocol schemas must be distinct")
        object.__setattr__(self, "schemas", values)


class RpcSocket(Generic[_IncomingT, _OutgoingT]):
    """An accepted WebSocket with raw frames or annotation-validated JSON messages.

    Iteration ends when the connection closes. Canon cancels the handler when
    the client disconnects or the connection closes, so ``finally`` blocks and
    context managers release upstream resources.
    """

    __slots__ = (
        "_connection",
        "_disconnected",
        "_lock",
        "_messages",
        "_message_contract",
        "_protocol",
        "_activity",
        "_queue",
        "_request_close",
        "_send_timeout",
        "_socket",
    )

    _lock: asyncio.Lock

    def __init__(self) -> None:
        raise TypeError(
            "RpcSocket instances are created by fastapi_canon.integrations.websockets"
        )

    @classmethod
    def _create(
        cls,
        socket: RpcWebSocket,
        connection: RpcConnection,
        messages: tuple[RpcMessageType, ...],
        protocol: RpcSocketProtocol | None,
        message_contract: SocketMessages | None,
        limits: RpcLimits,
        activity: _Activity | None,
        request_close: Callable[[RpcConnectionClose, str], None],
        disconnected: Callable[[RpcDisconnect], None],
    ) -> Self:
        self = cls.__new__(cls)
        self._socket = socket
        self._connection = connection
        self._messages = messages
        self._protocol = protocol
        self._message_contract = message_contract
        self._activity = activity
        self._send_timeout = limits.send_timeout
        self._queue = asyncio.Queue[_IncomingT](limits.max_queue_size)
        self._lock = asyncio.Lock()
        self._request_close = request_close
        self._disconnected = disconnected
        return self

    @property
    def messages(self) -> tuple[RpcMessageType, ...]:
        """The message types this endpoint declared."""
        return self._messages

    @property
    def protocol(self) -> RpcSocketProtocol | None:
        """The application protocol this endpoint declared, if any."""
        return self._protocol

    @property
    def closed(self) -> bool:
        return self._connection.closed or self._connection.close_code is not None

    async def receive(self) -> _IncomingT:
        """Receive a validated JSON value, or unchanged text/binary for raw sockets."""
        if self.closed and self._queue.empty():
            raise RpcDisconnect(
                self._connection.close_code or RpcConnectionClose.NORMAL,
                self._connection.close_reason,
            )
        return await self._queue.get()

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> _IncomingT:
        try:
            return await self.receive()
        except RpcDisconnect:
            raise StopAsyncIteration from None

    async def send(self, message: _OutgoingT) -> None:
        """Validate and serialize typed JSON, then wait on socket backpressure."""
        frame: Any = message
        if self._message_contract is not None:
            try:
                frame = self._message_contract.send(message)
            except (ValidationError, PydanticSerializationError):
                self._request_close(
                    RpcConnectionClose.INTERNAL_ERROR, "Invalid outgoing socket message"
                )
                raise RpcDisconnect(
                    RpcConnectionClose.INTERNAL_ERROR, "Invalid outgoing socket message"
                ) from None
        if isinstance(frame, str):
            message_type = RpcMessageType.TEXT
        elif isinstance(frame, bytes | bytearray | memoryview):
            message_type = RpcMessageType.BINARY
        else:
            raise TypeError("RpcSocket.send() expects str or bytes")
        if message_type not in self._messages:
            raise TypeError(f"This socket does not send {message_type} messages")
        async with self._lock:
            if self.closed:
                raise RpcDisconnect(reason="The socket is closed")
            try:
                async with asyncio.timeout(self._send_timeout):
                    if isinstance(frame, str):
                        await self._socket.send(frame)
                        size = len(frame.encode())
                    else:
                        data = bytes(frame)
                        await self._socket.send_bytes(data)
                        size = len(data)
            except RpcDisconnect as error:
                self._disconnected(error)
                raise
            except TimeoutError:
                self._request_close(
                    RpcConnectionClose.POLICY_VIOLATION, "Client too slow"
                )
                await notify_activity(
                    self._activity, "slow_consumer_closed", self._connection
                )
                raise RpcDisconnect(
                    RpcConnectionClose.POLICY_VIOLATION, "Client too slow"
                ) from None
        await notify_activity(
            self._activity, "stream_frame_sent", self._connection, size
        )

    async def close(
        self,
        close: RpcConnectionClose = RpcConnectionClose.NORMAL,
        *,
        reason: str = "",
    ) -> None:
        """Close the connection and cancel its handler."""
        await self._connection.close(close, reason=reason)

    async def _put(self, message: RpcSocketMessage) -> bool:
        value: Any = message
        if self._message_contract is not None:
            try:
                value = self._message_contract.receive(message)
            except ValidationError:
                self._request_close(
                    RpcConnectionClose.PROTOCOL_ERROR, "Invalid incoming socket message"
                )
                return False
        await self._queue.put(cast(_IncomingT, value))
        return True
