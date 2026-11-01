import asyncio
import importlib
import json
import sys
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi_canon.integrations.websockets.codegen import generate_python_client
from fastapi_canon.integrations.websockets.codegen.python import PythonClientOptions

PACKAGE = "connect_client"


class FakeSocket:
    """Answers every request with the same result, over JSON text frames."""

    def __init__(self, url: str, result: Any) -> None:
        self.url = url
        self.closed = False
        self._result = result
        self._replies: asyncio.Queue[str] = asyncio.Queue()

    async def send(self, message: str) -> None:
        request = json.loads(message)
        self._replies.put_nowait(
            json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": self._result})
        )

    async def recv(self) -> str:
        message = await self._replies.get()
        if isinstance(message, BaseException):
            raise message
        return message

    async def close(self) -> None:
        self.closed = True


class FakeNetwork:
    def __init__(self, result: Any = None) -> None:
        self.sockets: list[FakeSocket] = []
        self.headers: list[dict[str, str] | None] = []
        self.result = result

    async def __call__(
        self,
        url: str,
        *,
        subprotocols: list[str] | None,
        additional_headers: dict[str, str] | None,
    ) -> FakeSocket:
        socket = FakeSocket(url, self.result)
        self.sockets.append(socket)
        self.headers.append(additional_headers)
        return socket

    @property
    def urls(self) -> list[str]:
        return [socket.url for socket in self.sockets]


@pytest.fixture(scope="module")
def client_module(
    document: dict[str, Any],
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[ModuleType]:
    deployed = deepcopy(document)
    deployed["methods"] = [deployed["methods"][0]]
    deployed["methods"][0]["servers"] = [{"name": "primary"}]
    deployed["x-rpc-notifications"] = []
    deployed["servers"] = [
        {
            "name": "primary",
            "url": "wss://{host}/rpc",
            "variables": {"host": {"default": "api.example.com"}},
            "x-rpckit-transport": {"type": "websocket", "messageEncoding": "json"},
        },
        {
            "name": "secondary",
            "url": "wss://{host}/second",
            "variables": {"host": {"default": "api.example.com"}},
            "x-rpckit-transport": {"type": "websocket", "messageEncoding": "json"},
        },
    ]
    deployed["x-rpckit-binary-streams"] = [
        {
            "name": "greeting.frames",
            "url": "wss://{host}/frames",
            "direction": "server-to-client",
            "contentType": "image/jpeg",
            "frameType": "binary",
            "variables": {"host": {"default": "api.example.com"}},
        }
    ]
    root = tmp_path_factory.mktemp("connect")
    generate_python_client(
        deployed,
        root / PACKAGE,
        PythonClientOptions(
            package=PACKAGE,
            client_name="GreetingClient",
            with_transport="websocket",
        ),
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


async def test_notifications_without_listener_do_not_close_transport(
    client_module: ModuleType,
) -> None:
    network = FakeNetwork(result={"text": "ok"})
    transport = await client_module.WebSocketTransport.open(
        "wss://api.example.com/rpc",
        socket_factory=network,
        notification_queue_size=2,
    )
    try:
        socket = network.sockets[0]
        for _ in range(1000):
            socket._replies.put_nowait(
                json.dumps({"jsonrpc": "2.0", "method": "greeting.changed"})
            )
        assert await transport.request("greeting.say") == {"text": "ok"}
        assert not socket.closed
    finally:
        await transport.close()


async def test_connect_opens_a_socket_only_for_the_server_that_is_used(
    client_module: ModuleType,
) -> None:
    network = FakeNetwork({"text": "Hello, Mathis!"})

    async with client_module.GreetingClient.connect(
        host="stage.example.com",
        socket_factory=network,
        lazy=True,
    ) as client:
        assert network.urls == []

        result = await client.greeting.say(name="Mathis")

        assert result.text == "Hello, Mathis!"
        assert network.urls == ["wss://stage.example.com/rpc"]

    assert all(socket.closed for socket in network.sockets)


async def test_connecting_eagerly_opens_every_declared_server(
    client_module: ModuleType,
) -> None:
    network = FakeNetwork()

    async with client_module.GreetingClient.connect(
        host="stage.example.com",
        socket_factory=network,
    ):
        assert sorted(network.urls) == [
            "wss://stage.example.com/rpc",
            "wss://stage.example.com/second",
        ]


async def test_closed_signal_reports_unexpected_transport_failure(
    client_module: ModuleType,
) -> None:
    network = FakeNetwork()
    client = await client_module.GreetingClient.connect(socket_factory=network)
    try:
        assert not client.closed.done()
        network.sockets[0]._replies.put_nowait("not json")
        failure = await asyncio.wait_for(client.closed, 1)
        assert isinstance(failure, client_module.RpcTransportError)
    finally:
        await client.close()


async def test_closed_signal_completes_on_explicit_close(
    client_module: ModuleType,
) -> None:
    client = await client_module.GreetingClient.connect(socket_factory=FakeNetwork())
    await client.close()
    assert await asyncio.wait_for(client.closed, 1) is None


async def test_reconnect_opens_a_fresh_socket_after_disconnect(
    client_module: ModuleType,
) -> None:
    network = FakeNetwork({"text": "ok"})
    recovered = asyncio.Event()

    def changed(server, state, error):
        if state == "connected" and len(network.sockets) == 3:
            recovered.set()

    client = await client_module.GreetingClient.connect(
        socket_factory=network,
        reconnect=True,
        reconnect_initial_delay=0.001,
        on_state_change=changed,
    )
    try:
        assert (await client.greeting.say(name="first")).text == "ok"
        network.sockets[0]._replies.put_nowait(ConnectionResetError("dropped"))
        await asyncio.wait_for(recovered.wait(), 1)
        assert (await client.greeting.say(name="second")).text == "ok"
        async with asyncio.timeout(1):
            while len(network.sockets) < 3:
                await asyncio.sleep(0)
        assert not client.closed.done()
    finally:
        await client.close()


async def test_reconnecting_call_fails_after_the_request_timeout(
    client_module: ModuleType,
) -> None:
    network = FakeNetwork({"text": "ok"})
    reachable = True

    async def unreachable_later(
        url: str,
        *,
        subprotocols: list[str] | None,
        additional_headers: dict[str, str] | None,
    ) -> FakeSocket:
        if not reachable:
            raise OSError("server unreachable")
        return await network(
            url, subprotocols=subprotocols, additional_headers=additional_headers
        )

    client = await client_module.GreetingClient.connect(
        socket_factory=unreachable_later,
        reconnect=True,
        reconnect_initial_delay=0.001,
        request_timeout=0.05,
    )
    try:
        reachable = False
        network.sockets[0]._replies.put_nowait(ConnectionResetError("dropped"))
        async with asyncio.timeout(1):
            while client.state != client_module.RpcConnectionState.RECONNECTING:
                await asyncio.sleep(0)
        with pytest.raises(client_module.RpcRequestTimeoutError):
            await asyncio.wait_for(client.greeting.say(name="lost"), 1)
    finally:
        await client.close()


async def test_connect_forwards_headers_to_websocket_factories(
    client_module: ModuleType,
) -> None:
    network = FakeNetwork({"text": "Hello, Mathis!"})

    client = await client_module.GreetingClient.connect(
        socket_factory=network,
        headers={"Authorization": "Bearer secret"},
        lazy=True,
    )
    try:
        await client.greeting.say(name="Mathis")
    finally:
        await client.close()

    assert network.headers == [{"Authorization": "Bearer secret"}]


async def test_async_header_factory_refreshes_each_connection(
    client_module: ModuleType,
) -> None:
    network = FakeNetwork()
    calls = 0
    stream_headers: list[dict[str, str] | None] = []

    async def headers() -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"Authorization": f"Bearer {calls}"}

    async def stream_factory(
        url: str,
        *,
        subprotocols: list[str] | None,
        additional_headers: dict[str, str] | None,
    ) -> FakeSocket:
        stream_headers.append(additional_headers)
        return FakeSocket(url, None)

    async with (
        client_module.GreetingClient.connect(
            socket_factory=network,
            stream_socket_factory=stream_factory,
            headers=headers,
        ) as client,
        client.greeting.frames(),
    ):
        pass
    assert calls == 3
    assert {item["Authorization"] for item in network.headers if item} == {
        "Bearer 1",
        "Bearer 2",
    }
    assert stream_headers == [{"Authorization": "Bearer 3"}]


async def test_http_endpoint_override_is_converted_to_websocket(
    client_module: ModuleType,
) -> None:
    network = FakeNetwork({"text": "ok"})
    async with client_module.GreetingClient.connect(
        servers={client_module.ServerName.PRIMARY: "https://stage.example.com/rpc"},
        socket_factory=network,
        lazy=True,
    ) as client:
        await client.greeting.say(name="Mathis")
    assert network.urls == ["wss://stage.example.com/rpc"]


async def test_single_server_http_url_is_converted(
    document: dict[str, Any], tmp_path: Path
) -> None:
    package = "single_server_client"
    deployed = deepcopy(document)
    deployed["servers"] = [
        {
            "name": "primary",
            "url": "wss://api.example.com/rpc",
            "x-rpckit-transport": {"type": "websocket", "messageEncoding": "json"},
        }
    ]
    generate_python_client(
        deployed,
        tmp_path / package,
        PythonClientOptions(
            package=package, client_name="SingleClient", with_transport="websocket"
        ),
    )
    sys.path.insert(0, str(tmp_path))
    importlib.invalidate_caches()
    try:
        module = importlib.import_module(package)
        network = FakeNetwork()
        async with module.SingleClient.connect(
            url="https://stage.example.com/rpc", socket_factory=network
        ):
            assert network.urls == ["wss://stage.example.com/rpc"]
    finally:
        sys.path.remove(str(tmp_path))
        for name in list(sys.modules):
            if name == package or name.startswith(f"{package}."):
                del sys.modules[name]


async def test_a_single_server_can_be_pointed_somewhere_else(
    client_module: ModuleType,
) -> None:
    network = FakeNetwork({"text": "Hello, Mathis!"})

    async with client_module.GreetingClient.connect(
        host="stage.example.com",
        servers={client_module.ServerName.PRIMARY: "wss://localhost:8000/rpc"},
        socket_factory=network,
        lazy=True,
    ) as client:
        await client.greeting.say(name="Mathis")

        assert network.urls == ["wss://localhost:8000/rpc"]


def test_an_endpoint_override_must_match_its_server(
    client_module: ModuleType,
) -> None:
    mismatched = client_module.Endpoint(
        server=client_module.ServerName.SECONDARY,
        url="wss://secondary.example.com/rpc",
    )

    with pytest.raises(ValueError, match="Endpoint override.*declares"):
        client_module.GreetingClient.connect(
            servers={client_module.ServerName.PRIMARY: mismatched}
        )


async def test_binary_streams_reuse_the_host_the_client_connected_with(
    client_module: ModuleType,
) -> None:
    network = FakeNetwork()
    opened: list[str] = []

    async def stream_factory(
        url: str,
        *,
        subprotocols: list[str] | None,
        additional_headers: dict[str, str] | None,
    ) -> FakeSocket:
        opened.append(url)
        return FakeSocket(url, None)

    async with client_module.GreetingClient.connect(
        host="stage.example.com",
        socket_factory=network,
        stream_socket_factory=stream_factory,
    ) as client:
        async with client.greeting.frames():
            pass
        with pytest.raises(TypeError, match="host"):
            client.greeting.frames(host="other.example.com")

    assert opened == ["wss://stage.example.com/frames"]


async def test_with_transports_forwards_stream_variables(
    client_module: ModuleType,
) -> None:
    opened: list[str] = []

    async def stream_opener(endpoint: Any) -> Any:
        opened.append(endpoint.url)
        return FakeSocket(endpoint.url, None)

    client = client_module.GreetingClient.with_transports(
        {}, stream_opener=stream_opener, variables={"host": "stage.example.com"}
    )
    async with client.greeting.frames():
        pass
    assert opened == ["wss://stage.example.com/frames"]


async def test_an_unknown_stream_variable_is_rejected(
    client_module: ModuleType,
) -> None:
    streams = importlib.import_module(f"{PACKAGE}.streams")
    info = streams.BINARY_STREAMS[streams.BinaryStreamName.GREETING_FRAMES]

    with pytest.raises(ValueError, match="Unknown variables"):
        streams.resolve_stream_endpoint(info, values={"token": "secret"})
