import asyncio
from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from fastapi_canon.integrations.websockets import (
    Inject,
    ProtocolDefinitionError,
    RpcChannel,
    RpcConnectionClose,
    RpcErrorBinding,
    RpcHandshake,
    RpcLimits,
    RpcMessageType,
    RpcMiddleware,
    RpcRejection,
    RpcService,
    RpcSocket,
    RpcSocketEndpoint,
    RpcSocketProtocol,
)

from .testing import RpcTestClient, RpcTestConnectionClosed


class BrowserNotFound(Exception):
    pass


class CdpUnavailable(Exception):
    pass


BROWSER_NOT_FOUND = RpcErrorBinding(
    BrowserNotFound, rejection=RpcRejection.NOT_FOUND, message="Unknown browser"
)
CDP_UNAVAILABLE = RpcErrorBinding(
    CdpUnavailable,
    rejection=RpcRejection.INTERNAL_ERROR,
    message="Browser CDP unavailable",
)


class Upstream:
    """A fake upstream connection that echoes what the proxy forwards."""

    def __init__(self) -> None:
        self.inbox: asyncio.Queue[str | bytes] = asyncio.Queue()
        self.forwarded: list[str | bytes] = []
        self.opened = asyncio.Event()
        self.released = asyncio.Event()

    async def send(self, message: str | bytes) -> None:
        self.forwarded.append(message)
        if message == "fail":
            raise CdpUnavailable("ws://user:secret@chromium/devtools")
        if message == "missing":
            raise BrowserNotFound
        if message == "crash":
            raise RuntimeError("token=secret")
        if message != "quiet":
            await self.inbox.put(message)

    async def messages(self) -> AsyncIterator[str | bytes]:
        while True:
            message = await self.inbox.get()
            if message == "done":
                return
            yield message


async def relay(socket: RpcSocket, upstream: Upstream) -> None:
    async def to_upstream() -> None:
        async for message in socket:
            await upstream.send(message)

    upstream.opened.set()
    try:
        async with asyncio.TaskGroup() as group:
            forwarding = group.create_task(to_upstream())
            async for message in upstream.messages():
                await socket.send(message)
            forwarding.cancel()
    finally:
        upstream.released.set()


browser = RpcChannel("browser")


@browser.socket(
    "cdp",
    closes=(BROWSER_NOT_FOUND, CDP_UNAVAILABLE),
    summary="Proxy the Chrome DevTools Protocol.",
)
async def cdp(socket: Inject[RpcSocket], upstream: Inject[Upstream]) -> None:
    await relay(socket, upstream)


def _service(**options) -> RpcService:
    service = RpcService()
    service.socket("/browser/cdp", cdp, **options)
    return service


async def test_text_and_binary_messages_pass_through_unchanged() -> None:
    upstream = Upstream()
    payload = bytes(range(256))
    async with RpcTestClient(_service(), "/browser/cdp", context=upstream) as client:
        await client.send_message('{"id": 1, "method": "Page.enable"}')
        await client.send_message(payload)
        await client.send_message("")
        assert await client.next_message() == '{"id": 1, "method": "Page.enable"}'
        assert await client.next_message() == payload
        assert await client.next_message() == ""
        await client.send_message("done")
        assert await client.closed() == (RpcConnectionClose.NORMAL, "")

    assert upstream.forwarded[:3] == ['{"id": 1, "method": "Page.enable"}', payload, ""]
    assert upstream.released.is_set()


@pytest.mark.parametrize(
    ("message", "close", "reason"),
    [
        ("missing", RpcConnectionClose.POLICY_VIOLATION, "Unknown browser"),
        ("fail", RpcConnectionClose.INTERNAL_ERROR, "Browser CDP unavailable"),
        ("crash", RpcConnectionClose.INTERNAL_ERROR, "Internal error"),
    ],
)
async def test_failures_close_with_declared_outcomes(
    message: str, close: RpcConnectionClose, reason: str
) -> None:
    upstream = Upstream()
    async with RpcTestClient(_service(), "/browser/cdp", context=upstream) as client:
        await client.send_message(message)
        assert await client.closed() == (close, reason)

    assert upstream.released.is_set()


async def test_a_catch_all_binding_configures_the_internal_error_reason() -> None:
    channel = RpcChannel("browser")

    @channel.socket(
        "cdp",
        closes=(
            RpcErrorBinding(
                Exception,
                code="cdp_unavailable",
                rejection=RpcRejection.INTERNAL_ERROR,
                message="Browser CDP unavailable",
            ),
        ),
    )
    async def proxy(socket: Inject[RpcSocket]) -> None:
        raise RuntimeError("ws://user:secret@chromium")

    service = RpcService()
    service.socket("/cdp", proxy)
    async with RpcTestClient(service, "/cdp") as client:
        assert await client.closed() == (
            RpcConnectionClose.INTERNAL_ERROR,
            "Browser CDP unavailable",
        )


async def test_disconnect_cancels_forwarding_and_releases_the_upstream() -> None:
    upstream = Upstream()
    async with RpcTestClient(_service(), "/browser/cdp", context=upstream) as client:
        await client.send_message("quiet")
        await upstream.opened.wait()
        await client.socket.client_disconnect(1001)
        await asyncio.wait_for(upstream.released.wait(), 1)
        assert await client.closed() is None


async def test_dependency_failures_after_acceptance_use_declared_closes() -> None:
    def resolve(dependency: type) -> object:
        raise BrowserNotFound

    async with RpcTestClient(_service(), "/browser/cdp", resolver=resolve) as client:
        assert client.socket.accepted
        assert await client.closed() == (
            RpcConnectionClose.POLICY_VIOLATION,
            "Unknown browser",
        )


async def test_pre_acceptance_failures_reject_the_handshake() -> None:
    async def authenticate(handshake: RpcHandshake) -> None:
        raise BrowserNotFound

    service = _service(before_accept=authenticate, rejects=(BROWSER_NOT_FOUND,))
    async with RpcTestClient(service, "/browser/cdp", context=Upstream()) as client:
        await client.closed()
        assert not client.socket.accepted
        assert client.socket.rejection == (RpcRejection.NOT_FOUND, "Unknown browser")


async def test_handlers_close_with_explicit_codes() -> None:
    channel = RpcChannel("browser")
    cleaned = asyncio.Event()

    @channel.socket("cdp")
    async def proxy(socket: Inject[RpcSocket]) -> None:
        try:
            await socket.close(RpcConnectionClose.POLICY_VIOLATION, reason="Busy")
            await asyncio.sleep(10)
        finally:
            cleaned.set()

    service = RpcService()
    service.socket("/cdp", proxy)
    async with RpcTestClient(service, "/cdp") as client:
        assert await client.closed() == (RpcConnectionClose.POLICY_VIOLATION, "Busy")
        assert cleaned.is_set()


async def test_undeclared_message_types_are_rejected() -> None:
    channel = RpcChannel("browser")
    sent: list[Exception] = []

    @channel.socket("cdp", messages=(RpcMessageType.TEXT,))
    async def proxy(socket: Inject[RpcSocket]) -> None:
        try:
            await socket.send(b"binary")
        except TypeError as error:
            sent.append(error)
        async for _ in socket:
            pass

    service = RpcService()
    service.socket("/cdp", proxy)
    async with RpcTestClient(service, "/cdp") as client:
        await client.send_message(b"binary")
        assert await client.closed() == (
            RpcConnectionClose.PROTOCOL_ERROR,
            "Unsupported binary message",
        )
    assert len(sent) == 1


async def test_messages_above_the_limit_close_the_socket() -> None:
    service = _service(limits=RpcLimits(max_message_bytes=4))
    async with RpcTestClient(service, "/browser/cdp", context=Upstream()) as client:
        await client.send_message("too long")
        assert await client.closed() == (RpcConnectionClose.MESSAGE_TOO_BIG, "")


async def test_middleware_counts_messages_in_both_directions() -> None:
    class Counter(RpcMiddleware):
        def __init__(self) -> None:
            self.sent: list[int] = []
            self.received: list[int] = []

        async def stream_frame_sent(self, connection, size: int) -> None:
            self.sent.append(size)

        async def stream_frame_received(self, connection, size: int) -> None:
            self.received.append(size)

    counter = Counter()
    service = _service(middleware=[counter])
    async with RpcTestClient(service, "/browser/cdp", context=Upstream()) as client:
        await client.send_message("äb")
        assert await client.next_message() == "äb"
        await client.send_message("done")
        await client.closed()

    assert counter.received == [3, 4]
    assert counter.sent == [3]


async def test_path_variables_are_validated_before_acceptance() -> None:
    channel = RpcChannel("browser")
    seen: list[UUID] = []

    @channel.socket("page")
    async def page(browser_id: UUID, socket: Inject[RpcSocket]) -> None:
        seen.append(browser_id)

    service = RpcService()
    service.socket("/browsers/{browser_id}/cdp", page)
    browser_id = UUID(int=1)
    async with RpcTestClient(service, f"/browsers/{browser_id}/cdp") as client:
        assert await client.closed() == (RpcConnectionClose.NORMAL, "")
    async with RpcTestClient(service, "/browsers/nope/cdp") as client:
        with pytest.raises(RpcTestConnectionClosed):
            await client.next_message()
        assert client.socket.rejection == (
            RpcRejection.NOT_FOUND,
            "Invalid path variable",
        )
    assert seen == [browser_id]


def test_contract_exports_sockets_outside_json_rpc_methods() -> None:
    document = (
        _service(subprotocol="cdp")
        .contract(title="Browser", base_url="wss://{host}")
        .to_openrpc()
    )

    assert document["methods"] == []
    assert document["x-rpckit-sockets"] == [
        {
            "name": "browser.cdp",
            "url": "wss://{host}/browser/cdp",
            "messageTypes": ["text", "binary"],
            "summary": "Proxy the Chrome DevTools Protocol.",
            "variables": {"host": {"default": "{host}"}},
            "subprotocols": ["cdp"],
            "closes": [
                {
                    "code": 1008,
                    "x-rpckit-code": "browser_not_found",
                    "reason": "Unknown browser",
                },
                {
                    "code": 1011,
                    "x-rpckit-code": "cdp_unavailable",
                    "reason": "Browser CDP unavailable",
                },
            ],
        }
    ]


def test_sockets_must_inject_the_socket_once() -> None:
    channel = RpcChannel("browser")

    with pytest.raises(ProtocolDefinitionError, match="exactly once"):

        @channel.socket()
        async def missing() -> None: ...


def test_sockets_must_be_coroutines_returning_none() -> None:
    channel = RpcChannel("browser")

    with pytest.raises(ProtocolDefinitionError, match="async function"):

        @channel.socket()
        def blocking(socket: Inject[RpcSocket]) -> None: ...

    with pytest.raises(ProtocolDefinitionError, match="return None"):

        @channel.socket()
        async def returning(socket: Inject[RpcSocket]) -> bytes:
            return b""


def test_socket_declarations_are_validated() -> None:
    channel = RpcChannel("browser")

    with pytest.raises(ProtocolDefinitionError, match="at least one"):
        channel.socket(messages=())

    with pytest.raises(ProtocolDefinitionError, match="RpcMessageType"):
        channel.socket(messages=("json",))

    with pytest.raises(ProtocolDefinitionError, match="with a rejection"):

        @channel.socket(closes=(RpcErrorBinding(BrowserNotFound),))
        async def unbound(socket: Inject[RpcSocket]) -> None: ...


def test_socket_path_parameters_must_be_path_variables() -> None:
    channel = RpcChannel("browser")

    @channel.socket()
    async def page(browser_id: str, socket: Inject[RpcSocket]) -> None: ...

    with pytest.raises(ProtocolDefinitionError, match="browser_id"):
        RpcService().socket("/cdp", page)


def test_sockets_can_only_be_mounted_once() -> None:
    service = _service()

    with pytest.raises(ProtocolDefinitionError, match="mounted once"):
        service.socket("/other", cdp)


def test_socket_names_share_the_operation_namespace() -> None:
    channel = RpcChannel("browser")

    @channel.method("cdp")
    async def method() -> None: ...

    with pytest.raises(ProtocolDefinitionError, match="Duplicate RPC route"):

        @channel.socket("cdp")
        async def socket(socket: Inject[RpcSocket]) -> None: ...


def test_service_sockets_take_channels_or_a_socket_handler() -> None:
    channel = RpcChannel("terminal")

    @channel.socket()
    async def shell(socket: Inject[RpcSocket]) -> None: ...

    async def undecorated(socket: Inject[RpcSocket]) -> None: ...

    service = RpcService()
    with pytest.raises(ProtocolDefinitionError, match="channels=... or a"):
        service.socket("/rpc")
    with pytest.raises(ProtocolDefinitionError, match="either channels"):
        service.socket("/shell", shell, channels=(RpcChannel("other"),))
    with pytest.raises(ProtocolDefinitionError, match="only apply to JSON-RPC"):
        service.socket("/shell", shell, close_when_events_complete=True)
    with pytest.raises(ProtocolDefinitionError, match=r"@channel\.socket\(\)"):
        service.socket("/shell", undecorated)

    endpoint = service.socket("/shell", shell)

    assert isinstance(endpoint, RpcSocketEndpoint)
    assert endpoint.socket.name == "terminal.shell"


CDP = RpcSocketProtocol(
    name="cdp",
    version="1.3",
    schemas=("browser_protocol.json", "js_protocol.json"),
)


async def test_a_declared_protocol_is_exported_and_visible_to_the_handler() -> None:
    channel = RpcChannel("browser")
    seen: list[RpcSocketProtocol | None] = []

    @channel.socket("cdp", protocol=CDP)
    async def cdp(socket: Inject[RpcSocket]) -> None:
        seen.append(socket.protocol)
        for _ in range(2):
            await socket.send(await socket.receive())

    service = RpcService()
    service.socket("/browser/cdp", cdp)
    document = service.contract(title="Browser", base_url="wss://h").to_openrpc()

    assert document["x-rpckit-sockets"][0]["x-rpckit-protocol"] == {
        "name": "cdp",
        "version": "1.3",
        "encoding": "json",
        "schemas": ["browser_protocol.json", "js_protocol.json"],
    }
    async with RpcTestClient(service, "/browser/cdp") as client:
        await client.send_message("not JSON")
        await client.send_message(bytes(range(256)))
        assert await client.next_message() == "not JSON"
        assert await client.next_message() == bytes(range(256))
        assert await client.closed() == (RpcConnectionClose.NORMAL, "")
    assert seen == [CDP]


def test_sockets_without_a_protocol_export_no_protocol() -> None:
    document = _service().contract(title="Browser", base_url="wss://h").to_openrpc()

    assert "x-rpckit-protocol" not in document["x-rpckit-sockets"][0]


def test_protocol_declarations_are_validated() -> None:
    assert RpcSocketProtocol("graphql-ws", encoding="json").version is None
    assert RpcSocketProtocol("x", schemas=["a.json"]).schemas == ("a.json",)
    with pytest.raises(ValueError, match="lowercase tokens"):
        RpcSocketProtocol("CDP")
    with pytest.raises(ValueError, match="versions"):
        RpcSocketProtocol("cdp", version=" 1.3")
    with pytest.raises(ValueError, match="encodings"):
        RpcSocketProtocol("cdp", encoding="JSON")
    with pytest.raises(TypeError, match="sequence of strings"):
        RpcSocketProtocol("cdp", schemas="schema.json")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="without whitespace"):
        RpcSocketProtocol("cdp", schemas=("a b.json",))
    with pytest.raises(ValueError, match="distinct"):
        RpcSocketProtocol("cdp", schemas=("a.json", "a.json"))
    with pytest.raises(ProtocolDefinitionError, match="RpcSocketProtocol"):
        RpcChannel("browser").socket(protocol="cdp")  # type: ignore[arg-type]
