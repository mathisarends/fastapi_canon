import asyncio
import importlib
import sys
from collections.abc import Iterator
from types import ModuleType
from urllib.parse import urlsplit

import pytest
from fastapi_canon.integrations.websockets import (
    Inject,
    RpcChannel,
    RpcConnectionClose,
    RpcDisconnect,
    RpcErrorBinding,
    RpcMessageType,
    RpcRejection,
    RpcService,
    RpcSocket,
    RpcSocketProtocol,
)
from fastapi_canon.integrations.websockets.codegen import generate_python_client
from fastapi_canon.integrations.websockets.codegen.python import PythonClientOptions
from fastapi_canon.integrations.websockets.websocket import (
    CLOSE_CODES,
    REJECTION_CLOSE_CODES,
)

from ..testing import InMemorySocket

PACKAGE = "browser_client"


class BrowserNotFound(Exception):
    pass


browser = RpcChannel("browser")
tabs = browser.child("tabs")


@browser.socket(
    "cdp",
    closes=(
        RpcErrorBinding(
            BrowserNotFound, rejection=RpcRejection.NOT_FOUND, message="Unknown browser"
        ),
    ),
)
async def cdp(socket: Inject[RpcSocket]) -> None:
    async for message in socket:
        if message == "done":
            return
        if message == "missing":
            raise BrowserNotFound
        if message == "crash":
            raise RuntimeError("ws://user:secret@chromium")
        await socket.send(message)


@tabs.socket(
    "cdp",
    protocol=RpcSocketProtocol("cdp", version="1.3", schemas=("cdp.json",)),
    messages=(RpcMessageType.TEXT,),
)
async def tab_cdp(tab_id: str, socket: Inject[RpcSocket]) -> None:
    await socket.send(tab_id)


service = RpcService()
service.socket("/browser/cdp", cdp)
service.socket("/browser/tabs/{tab_id}/cdp", tab_cdp, name="tab-cdp")


@pytest.fixture(scope="module")
def client_package(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ModuleType]:
    root = tmp_path_factory.mktemp("socket_client")
    document = service.contract(title="Browser", base_url="ws://localhost").to_openrpc()
    generate_python_client(
        document,
        root / PACKAGE,
        PythonClientOptions(package=PACKAGE, client_name="BrowserClient"),
    )
    sys.path.insert(0, str(root))
    importlib.invalidate_caches()
    try:
        yield importlib.import_module(PACKAGE)
    finally:
        sys.path.remove(str(root))
        for name in [
            name
            for name in sys.modules
            if name == PACKAGE or name.startswith(f"{PACKAGE}.")
        ]:
            del sys.modules[name]


class LoopbackSocket:
    """A raw socket transport served by the real runtime over an in-memory socket."""

    def __init__(
        self, package: ModuleType, path: str, rpc: RpcService = service
    ) -> None:
        self._package = package
        self.socket = InMemorySocket(path)
        self.task = asyncio.create_task(rpc.serve(self.socket))
        self._closed: Exception | None = None

    async def receive(self) -> str | bytes:
        if self._closed is not None:
            raise self._closed
        try:
            return await self.socket.client_receive()
        except RpcDisconnect:
            self._closed = self._close_error()
            raise self._closed from None

    def _close_error(self) -> Exception:
        if self.socket.rejection is not None:
            code = REJECTION_CLOSE_CODES[self.socket.rejection[0]]
            reason = self.socket.rejection[1]
        else:
            close, reason = self.socket.closed or (RpcConnectionClose.NORMAL, "")
            code = CLOSE_CODES[close]
        if code == 1000:
            return self._package.RpcStreamClosed("closed")
        failure = (
            self._package.RpcStreamRefused
            if code == 1008
            else self._package.RpcStreamFailed
        )
        return failure(code, reason)

    async def send(self, message: str | bytes) -> None:
        await self.socket.client_send(message)

    async def close(self) -> None:
        await self.socket.client_disconnect()
        await self.task


class UnusedTransport:
    async def close(self) -> None: ...


def _client(package: ModuleType, opened: list | None = None):
    async def opener(endpoint):
        socket = LoopbackSocket(package, urlsplit(endpoint.url).path)
        if opened is not None:
            opened.append((endpoint, socket))
        return socket

    return package.BrowserClient.with_transports(
        UnusedTransport(), socket_opener=opener
    )


async def test_mixed_messages_round_trip_until_a_normal_close(
    client_package: ModuleType,
) -> None:
    opened: list = []
    client = _client(client_package, opened)

    async with client.browser.cdp() as socket:
        assert isinstance(socket, client_package.Socket)
        await socket.send('{"id":1,"method":"Page.enable"}')
        await socket.send(bytearray(b"\x00\xff"))
        assert await socket.receive() == '{"id":1,"method":"Page.enable"}'
        assert await socket.receive() == b"\x00\xff"
        await socket.send("done")
        assert [message async for message in socket] == []
        with pytest.raises(client_package.RpcStreamClosed):
            await socket.receive()

    endpoint, loopback = opened[0]
    assert endpoint.name is client_package.SocketName.BROWSER_CDP
    assert endpoint.url == "ws://localhost/browser/cdp"
    assert endpoint.message_types == ("text", "binary")
    assert endpoint.protocol is None
    assert loopback.socket.closed == (RpcConnectionClose.NORMAL, "")


@pytest.mark.parametrize(
    ("message", "failure", "code", "reason"),
    [
        ("missing", "RpcStreamRefused", 1008, "Unknown browser"),
        ("crash", "RpcStreamFailed", 1011, "Internal error"),
    ],
)
async def test_server_closes_surface_as_transport_errors(
    client_package: ModuleType, message: str, failure: str, code: int, reason: str
) -> None:
    client = _client(client_package)

    with pytest.raises(getattr(client_package, failure)) as raised:
        async with client.browser.cdp() as socket:
            await socket.send(message)
            await socket.receive()

    assert (raised.value.code, raised.value.reason) == (code, reason)


async def test_call_variables_fill_the_socket_url(client_package: ModuleType) -> None:
    opened: list = []
    client = _client(client_package, opened)

    async with client.browser.tabs.cdp(tab_id="tab-7") as socket:
        assert await socket.receive() == "tab-7"
        with pytest.raises(TypeError, match="binary"):
            await socket.send(b"binary")

    endpoint, _ = opened[0]
    assert endpoint.url == "ws://localhost/browser/tabs/tab-7/cdp"
    assert endpoint.message_types == ("text",)
    assert endpoint.protocol == client_package.SocketProtocol(
        name="cdp", version="1.3", schemas=("cdp.json",)
    )


async def test_clients_without_an_opener_report_unavailable_sockets(
    client_package: ModuleType,
) -> None:
    client = client_package.BrowserClient.with_transports(UnusedTransport())

    with pytest.raises(
        client_package.RpcStreamsUnavailableError, match="socket opener"
    ):
        async with client.browser.cdp():
            pass
