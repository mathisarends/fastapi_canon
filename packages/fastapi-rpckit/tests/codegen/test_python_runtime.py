import asyncio
import importlib
import json
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from copy import deepcopy
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from fastapi_canon.integrations.websockets.codegen import generate_python_client
from fastapi_canon.integrations.websockets.codegen.python import PythonClientOptions
from pydantic import TypeAdapter


class StubTransport:
    def __init__(self, result: Any = None) -> None:
        self.result = result
        self.closed = 0

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        metadata=None,
    ) -> Any:
        return self.result

    async def notifications(self) -> AsyncIterator[dict[str, Any]]:
        if False:
            yield {}

    async def close(self) -> None:
        self.closed += 1


@pytest.fixture
def runtime(generated_client: ModuleType) -> ModuleType:
    return importlib.import_module(f"{generated_client.__name__}.internal")


async def test_client_core_wraps_response_validation_with_the_route(
    runtime: ModuleType,
) -> None:
    core = runtime.RpcClientCore(StubTransport("wrong"))
    route = runtime.RpcRouteInfo(
        method="math.count",
        result_adapter=TypeAdapter(int),
    )

    with pytest.raises(runtime.RpcResponseValidationError) as raised:
        await core.request(route)

    assert raised.value.method == "math.count"


async def test_client_core_closes_an_owned_transport_once(
    runtime: ModuleType,
) -> None:
    transport = StubTransport()
    core = runtime.RpcClientCore(transport)

    await core.close()
    await core.close()

    assert transport.closed == 1


def test_server_info_resolves_declared_variables(runtime: ModuleType) -> None:
    server = runtime.RpcServerInfo(
        name="control",
        url="wss://{host}/{sessionId}",
        variables={
            "host": runtime.RpcServerVariable("api.example.com"),
            "sessionId": runtime.RpcServerVariable("demo"),
        },
    )

    assert server.resolve({"sessionId": "s-123"}) == "wss://api.example.com/s-123"


def test_server_info_rejects_unknown_variables(runtime: ModuleType) -> None:
    server = runtime.RpcServerInfo(name="control", url="wss://example.com")

    with pytest.raises(ValueError, match="Unknown variables"):
        server.resolve({"token": "secret"})


async def test_binary_websocket_stream_sends_and_receives_raw_frames(
    document: dict[str, Any], tmp_path
) -> None:
    deployed = deepcopy(document)
    deployed["servers"] = [
        {
            "name": "control",
            "url": "wss://api/rpc",
            "x-rpckit-transport": {
                "type": "websocket",
                "messageEncoding": "json",
            },
        }
    ]
    deployed["x-rpckit-binary-streams"] = [
        {
            "name": "voice",
            "url": "wss://media/{sessionId}",
            "direction": "server-to-client",
            "contentType": "audio/pcm;rate=24000",
            "frameType": "binary",
            "variables": {"sessionId": {"default": "demo"}},
        }
    ]
    package = "voice_client"
    generate_python_client(
        deployed,
        tmp_path / package,
        PythonClientOptions(package=package, with_transport="websocket"),
    )
    sys.path.insert(0, str(tmp_path))

    class Socket:
        def __init__(self) -> None:
            self.sent: list[bytes] = []
            self.closed = False

        async def send(self, message: bytes) -> None:
            self.sent.append(message)

        async def recv(self) -> bytes:
            return b"assistant-pcm"

        async def close(self) -> None:
            self.closed = True

    socket = Socket()

    async def factory(
        url: str,
        *,
        subprotocols: list[str] | None,
        additional_headers: dict[str, str] | None,
    ):
        assert url == "wss://media/turn-1"
        assert subprotocols is None
        assert additional_headers is None
        return socket

    try:
        media = importlib.import_module(f"{package}.streams")
        endpoint = media.BinaryStreamEndpoint(
            name=media.BinaryStreamName.VOICE,
            url="wss://media/turn-1",
            content_type="audio/pcm;rate=24000",
        )
        stream = await media.BinaryWebSocketStream.open(
            endpoint, socket_factory=factory
        )

        assert await stream.receive() == b"assistant-pcm"
        await stream.close()

        assert socket.sent == []
        assert socket.closed is True
    finally:
        sys.path.remove(str(tmp_path))
        for name in [
            name
            for name in sys.modules
            if name == package or name.startswith(f"{package}.")
        ]:
            del sys.modules[name]


class ClosingTransport:
    """A transport whose notification stream ends right away."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.closed = 0

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        metadata=None,
    ) -> Any:
        return None

    async def notifications(self) -> AsyncIterator[dict[str, Any]]:
        if self.error is not None:
            raise self.error
        if False:
            yield {}

    async def close(self) -> None:
        self.closed += 1


async def _drain(stream: AsyncIterator[dict[str, Any]]) -> list[dict[str, Any]]:
    return [message async for message in stream]


async def test_subscribing_after_the_pump_ended_stops_instead_of_hanging(
    runtime: ModuleType,
) -> None:
    core = runtime.RpcClientCore(ClosingTransport())

    assert await _drain(core.notifications("tasks.updated")) == []
    assert await _drain(core.notifications("tasks.updated")) == []


async def test_subscribing_after_the_pump_failed_reports_the_failure(
    runtime: ModuleType,
) -> None:
    core = runtime.RpcClientCore(ClosingTransport(RuntimeError("socket died")))

    with pytest.raises(runtime.RpcTransportError):
        await _drain(core.notifications("tasks.updated"))
    with pytest.raises(runtime.RpcTransportError):
        await _drain(core.notifications("tasks.updated"))


@pytest.fixture(scope="module")
def websocket_runtime(document, tmp_path_factory) -> Iterator[ModuleType]:
    root = tmp_path_factory.mktemp("websocket_runtime")
    package = "transport_runtime_client"
    deployed = deepcopy(document)
    deployed["servers"] = [
        {
            "name": "rpc",
            "url": "ws://test",
            "x-rpckit-transport": {"type": "websocket", "messageEncoding": "json"},
        }
    ]
    generate_python_client(
        deployed,
        root / package,
        PythonClientOptions(package=package, with_transport="websocket"),
    )
    sys.path.insert(0, str(root))
    try:
        yield importlib.import_module(package)
    finally:
        sys.path.remove(str(root))
        for name in list(sys.modules):
            if name == package or name.startswith(f"{package}."):
                del sys.modules[name]


class ControlledSocket:
    def __init__(self) -> None:
        self.inbox: asyncio.Queue[str] = asyncio.Queue()
        self.sent: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.send_gate = asyncio.Event()
        self.send_gate.set()
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.put_nowait(json.loads(message))
        await self.send_gate.wait()

    async def recv(self) -> str:
        return await self.inbox.get()

    async def close(self) -> None:
        self.closed = True

    def reply(self, request_id: int, **payload: Any) -> None:
        self.inbox.put_nowait(
            json.dumps({"jsonrpc": "2.0", "id": request_id, **payload})
        )


@pytest.mark.parametrize("abandon", ["timeout", "cancel"])
async def test_late_duplicate_and_unknown_responses_leave_other_calls_alive(
    websocket_runtime: ModuleType, abandon: str
) -> None:
    socket = ControlledSocket()
    transport = websocket_runtime.WebSocketTransport(socket, request_timeout=None)
    try:
        if abandon == "timeout":
            transport._request_timeout = 0
        abandoned = asyncio.create_task(transport.request("slow"))
        first = await socket.sent.get()
        if abandon == "timeout":
            with pytest.raises(websocket_runtime.RpcTransportError, match="timed out"):
                await abandoned
            transport._request_timeout = None
        else:
            abandoned.cancel()
            with pytest.raises(asyncio.CancelledError):
                await abandoned
        assert not transport._pending

        control = await socket.sent.get()
        assert control == {
            "jsonrpc": "2.0",
            "method": "$/cancelRequest",
            "params": {"id": first["id"]},
        }

        other = asyncio.create_task(transport.request("fast"))
        second = await socket.sent.get()
        socket.reply(first["id"], result="late")
        socket.reply(123456, error={"code": -32601, "message": "unknown"})
        socket.reply(second["id"], result="ok")
        socket.reply(second["id"], result="duplicate")
        assert await other == "ok"
        last = asyncio.create_task(transport.request("later"))
        third = await socket.sent.get()
        socket.reply(third["id"], result="still connected")
        assert await last == "still connected"
        assert not transport.closed.done()
    finally:
        await transport.close()
    assert not transport._pending
    assert transport._reader.done()


class ClientDeadlines:
    def __init__(self, monkeypatch, runtime):
        self.created = asyncio.Queue()
        module = importlib.import_module(runtime.WebSocketTransport.__module__)
        proxy = SimpleNamespace(**vars(asyncio))
        proxy.timeout = self.timeout
        monkeypatch.setattr(module, "asyncio", proxy)

    def timeout(self, delay):
        waiting = asyncio.timeout(None)
        if delay is not None:
            self.created.put_nowait((delay, waiting))
        return waiting

    async def next(self):
        return await asyncio.wait_for(self.created.get(), 1)

    def expire(self, waiting):
        waiting.reschedule(asyncio.get_running_loop().time())


async def test_cancel_before_request_starts_sends_no_frames(websocket_runtime):
    socket = ControlledSocket()
    transport = websocket_runtime.WebSocketTransport(socket, request_timeout=None)
    task = asyncio.create_task(transport.request("unsent"))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert socket.sent.empty()
    assert not transport._pending
    assert not transport._cancellations
    await transport.close()


async def test_best_effort_cancel_workers_have_bounded_capacity(
    websocket_runtime, monkeypatch
):
    module = importlib.import_module(websocket_runtime.WebSocketTransport.__module__)
    monkeypatch.setattr(module, "_MAX_CANCEL_SENDS", 2)
    socket = ControlledSocket()
    transport = websocket_runtime.WebSocketTransport(socket, request_timeout=None)
    tasks = [asyncio.create_task(transport.request("work")) for _ in range(3)]
    for _ in tasks:
        await socket.sent.get()
    socket.send_gate.clear()
    for task in tasks:
        task.cancel()
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(e, asyncio.CancelledError) for e in outcomes)
    assert len(transport._cancellations) == 2
    await transport.close()
    assert not transport._cancellations
    assert not transport._pending


@pytest.mark.parametrize("ending", ["cancel", "timeout"])
async def test_abandoned_call_cancels_server_once_and_preserves_local_outcome(
    websocket_runtime, monkeypatch, ending
):
    from fastapi_canon.integrations.websockets import RpcChannel, RpcService
    from fastapi_canon.integrations.websockets.testing import InMemorySocketFactory

    before = asyncio.all_tasks()
    clock = ClientDeadlines(monkeypatch, websocket_runtime)
    channel = RpcChannel("cancellation")
    entered, stopped = asyncio.Event(), asyncio.Event()
    controls = []

    @channel.method()
    async def work() -> None:
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            stopped.set()
            raise

    @channel.method()
    async def ping() -> str:
        return "pong"

    def configure(socket, attempt):
        def record(frame):
            message = json.loads(frame)
            if message.get("method") == "$/cancelRequest":
                controls.append(message)
            return False

        socket.client_to_server.drop_when = record

    app = RpcService()
    app.socket("/rpc", channels=[channel])
    async with InMemorySocketFactory(app, configure=configure) as factory:
        transport = await websocket_runtime.WebSocketTransport.open(
            "ws://test/rpc", socket_factory=factory, request_timeout=5
        )
        try:
            task = asyncio.create_task(transport.request("cancellation.work"))
            duration, waiting = await clock.next()
            assert duration == 5
            await asyncio.wait_for(entered.wait(), 1)
            if ending == "cancel":
                task.cancel("user cancelled")
                with pytest.raises(asyncio.CancelledError, match="user cancelled"):
                    await task
            else:
                clock.expire(waiting)
                with pytest.raises(websocket_runtime.RpcRequestTimeoutError):
                    await task
            await asyncio.wait_for(stopped.wait(), 1)
            assert controls == [
                {"jsonrpc": "2.0", "method": "$/cancelRequest", "params": {"id": 1}}
            ]
            assert not transport._pending
            transport._request_timeout = None
            assert await transport.request("cancellation.ping") == "pong"
            assert not transport.closed.done()
        finally:
            await transport.close()
        assert not transport._cancellations
    assert not asyncio.all_tasks() - before


@pytest.mark.parametrize("ending", ["close", "disconnect", "expiry"])
async def test_background_cancel_send_is_bounded_and_cleaned_up(
    websocket_runtime, monkeypatch, ending
):
    before = asyncio.all_tasks()
    clock = ClientDeadlines(monkeypatch, websocket_runtime)
    socket = ControlledSocket()
    transport = websocket_runtime.WebSocketTransport(socket, request_timeout=None)
    task = asyncio.create_task(transport.request("work"))
    await socket.sent.get()
    socket.send_gate.clear()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    duration, waiting = await clock.next()
    assert duration == 1.0
    control = await socket.sent.get()
    assert control["method"] == "$/cancelRequest"
    sends = tuple(transport._cancellations)
    assert len(sends) == 1 and not sends[0].done()
    if ending == "expiry":
        clock.expire(waiting)
        await asyncio.wait_for(asyncio.gather(*sends), 1)
    elif ending == "disconnect":
        socket.inbox.put_nowait("invalid JSON")
        await transport.closed
    await transport.close()
    assert not transport._pending
    assert not transport._cancellations
    assert all(t.done() for t in sends)
    assert not asyncio.all_tasks() - before


async def test_cancel_send_failure_is_consumed_and_does_not_close_transport(
    websocket_runtime,
):
    class Socket(ControlledSocket):
        async def send(self, message):
            if json.loads(message)["method"] == "$/cancelRequest":
                raise RuntimeError("control cannot be sent")
            await super().send(message)

    socket = Socket()
    transport = websocket_runtime.WebSocketTransport(socket, request_timeout=None)
    try:
        task = asyncio.create_task(transport.request("work"))
        await socket.sent.get()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        sends = tuple(transport._cancellations)
        await asyncio.gather(*sends)
        await asyncio.sleep(0)  # Run the task's removal callback.
        assert not transport._cancellations
        assert not transport.closed.done()
    finally:
        await transport.close()


async def test_close_before_cancellation_skips_control_on_original_and_new_socket(
    websocket_runtime,
):
    socket = ControlledSocket()
    old = websocket_runtime.WebSocketTransport(socket, request_timeout=None)
    task = asyncio.create_task(old.request("work"))
    await socket.sent.get()
    await old.close()
    with pytest.raises(websocket_runtime.RpcConnectionLostError):
        await task
    new_socket = ControlledSocket()
    new = websocket_runtime.WebSocketTransport(new_socket, request_timeout=None)
    try:
        old._cancel_request(1)
        await asyncio.sleep(0)
        assert socket.sent.empty()
        assert new_socket.sent.empty()
        assert not old._cancellations
    finally:
        await new.close()


async def test_late_response_from_a_served_endpoint_keeps_the_client_usable(
    websocket_runtime: ModuleType,
) -> None:
    from fastapi_canon.integrations.websockets import RpcChannel, RpcService
    from fastapi_canon.integrations.websockets.testing import InMemorySocketFactory

    channel = RpcChannel("resilience")
    entered, release = asyncio.Event(), asyncio.Event()

    @channel.method
    async def slow() -> str:
        entered.set()
        await release.wait()
        return "late"

    @channel.method
    async def fast() -> str:
        return "ok"

    service = RpcService()
    service.socket("/rpc", channels=[channel])

    def configure(socket, attempt):
        socket.server_to_client.hold()

    async with InMemorySocketFactory(service, configure=configure) as factory:
        transport = await websocket_runtime.WebSocketTransport.open(
            "ws://test/rpc", socket_factory=factory, request_timeout=None
        )
        try:
            pending = asyncio.create_task(transport.request("resilience.slow"))
            await entered.wait()
            release.set()
            faults = factory.sockets[0].server_to_client
            await faults.held.wait()
            assert faults.held_count == 1
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            faults.release()
            transport._request_timeout = None
            assert await transport.request("resilience.fast") == "ok"
            assert await transport.request("resilience.fast") == "ok"
            assert not transport.closed.done()
        finally:
            release.set()
            await transport.close()
    assert all(task.done() for task in factory._tasks)


@pytest.mark.parametrize(
    "payload",
    [
        {"id": "unknown", "result": 1},
        {"id": True, "result": 1},
        {"id": 999},
        {"id": 999, "result": 1, "error": {"code": -1, "message": "bad"}},
        {"id": 999, "error": "bad"},
        {"id": 999, "error": {"code": "bad", "message": "bad"}},
    ],
)
async def test_malformed_unknown_responses_still_fail_pending_calls(
    websocket_runtime: ModuleType, payload: dict[str, Any]
) -> None:
    socket = ControlledSocket()
    transport = websocket_runtime.WebSocketTransport(socket, request_timeout=None)
    pending = asyncio.create_task(transport.request("pending"))
    await socket.sent.get()
    socket.inbox.put_nowait(json.dumps({"jsonrpc": "2.0", **payload}))
    with pytest.raises(websocket_runtime.RpcTransportError):
        await pending
    assert isinstance(await transport.closed, websocket_runtime.RpcTransportError)
    assert not transport._pending


async def test_blocked_send_is_inside_request_timeout_and_retires_transport(
    websocket_runtime: ModuleType,
) -> None:
    socket = ControlledSocket()
    socket.send_gate.clear()
    transport = websocket_runtime.WebSocketTransport(socket, request_timeout=0)
    with pytest.raises(websocket_runtime.RpcTransportError, match="timed out"):
        async with asyncio.timeout(1):
            await transport.request("blocked")
    assert socket.closed
    assert not transport._pending
    assert transport._reader.done()
    assert isinstance(await transport.closed, websocket_runtime.RpcTransportError)


async def test_cancel_during_send_preserves_cancellation_and_retires_transport(
    websocket_runtime: ModuleType,
) -> None:
    socket = ControlledSocket()
    socket.send_gate.clear()
    transport = websocket_runtime.WebSocketTransport(socket, request_timeout=None)
    request = asyncio.create_task(transport.request("blocked"))
    await socket.sent.get()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert socket.closed
    assert not transport._pending
    assert transport._reader.done()


async def test_deep_json_fails_all_client_calls_without_leaked_futures(
    websocket_runtime: ModuleType,
) -> None:
    socket = ControlledSocket()
    transport = websocket_runtime.WebSocketTransport(socket, request_timeout=None)
    calls = [asyncio.create_task(transport.request("pending")) for _ in range(2)]
    for _ in calls:
        await socket.sent.get()
    socket.inbox.put_nowait("[" * 100_000)
    outcomes = await asyncio.gather(*calls, return_exceptions=True)
    assert all(isinstance(e, websocket_runtime.RpcTransportError) for e in outcomes)
    assert isinstance(await transport.closed, websocket_runtime.RpcTransportError)
    assert not transport._pending
    assert transport._reader.done()
    assert socket.closed


async def test_integer_digit_limit_is_a_protocol_failure_not_connection_loss(
    websocket_runtime,
):
    previous = sys.get_int_max_str_digits()
    socket = ControlledSocket()
    transport = websocket_runtime.WebSocketTransport(socket, request_timeout=None)
    pending = asyncio.create_task(transport.request("pending"))
    try:
        await socket.sent.get()
        sys.set_int_max_str_digits(640)
        socket.inbox.put_nowait('{"jsonrpc":"2.0","id":1,"result":' + "9" * 641 + "}")
        with pytest.raises(websocket_runtime.RpcTransportError) as raised:
            await asyncio.wait_for(pending, 1)
        assert not isinstance(raised.value, websocket_runtime.RpcConnectionLostError)
        assert await transport.closed is raised.value
        assert not transport._pending
    finally:
        sys.set_int_max_str_digits(previous)
        await transport.close()


async def test_socket_reset_is_a_client_error_with_cause_and_sent_state(
    websocket_runtime,
):
    broken = asyncio.Event()
    original = ConnectionResetError("connection reset")

    class Socket(ControlledSocket):
        async def recv(self):
            await broken.wait()
            raise original

    socket = Socket()
    transport = websocket_runtime.WebSocketTransport(socket, request_timeout=None)
    call = asyncio.create_task(transport.request("inflight"))
    await socket.sent.get()
    broken.set()
    with pytest.raises(websocket_runtime.RpcClientError) as raised:
        await call
    assert isinstance(raised.value, websocket_runtime.RpcConnectionLostError)
    assert raised.value.sent is True
    assert raised.value.__cause__ is original
    closed = await transport.closed
    assert isinstance(closed, websocket_runtime.RpcConnectionLostError)
    assert closed.__cause__ is original
    with pytest.raises(websocket_runtime.RpcConnectionLostError) as unsent:
        await transport.request("never sent")
    assert unsent.value.sent is False
    assert socket.sent.empty()
    assert not transport._pending


async def test_send_that_delivers_then_raises_is_never_classified_unsent(
    websocket_runtime,
):
    original = ConnectionResetError("failed after delivery")

    class Socket(ControlledSocket):
        async def send(self, message):
            await super().send(message)
            raise original

    socket = Socket()
    transport = websocket_runtime.WebSocketTransport(socket, request_timeout=None)
    with pytest.raises(websocket_runtime.RpcClientError) as raised:
        await transport.request("delivered")
    assert isinstance(raised.value, websocket_runtime.RpcConnectionLostError)
    assert raised.value.sent is True
    assert raised.value.__cause__ is original
    assert (await socket.sent.get())["method"] == "delivered"
    assert socket.closed
    assert transport._reader.done()


async def test_request_timeout_has_method_and_timeout_fields(websocket_runtime):
    socket = ControlledSocket()
    transport = websocket_runtime.WebSocketTransport(socket, request_timeout=0)
    try:
        with pytest.raises(websocket_runtime.RpcTransportError) as raised:
            await transport.request("slow")
        assert isinstance(raised.value, websocket_runtime.RpcRequestTimeoutError)
        assert raised.value.method == "slow"
        assert raised.value.timeout == 0
        assert not transport.closed.done()
    finally:
        await transport.close()


async def test_unserializable_params_do_not_register_a_pending_request(
    websocket_runtime,
):
    socket = ControlledSocket()
    transport = websocket_runtime.WebSocketTransport(socket)
    try:
        with pytest.raises(websocket_runtime.RpcTransportError):
            await transport.request("invalid", {"value": object()})
        assert not transport._pending
        assert not transport._sent
        assert socket.sent.empty()
        assert not transport.closed.done()
    finally:
        await transport.close()


@pytest.mark.parametrize(
    "status,code,permanent",
    [
        (401, None, True),
        (403, None, True),
        (404, None, True),
        (503, None, False),
        (None, 1008, True),
        (None, None, False),
    ],
)
async def test_connect_errors_preserve_handshake_classification(
    websocket_runtime, status, code, permanent
):
    from types import SimpleNamespace

    original = OSError("handshake failed")
    original.response = SimpleNamespace(status_code=status)
    original.rcvd = SimpleNamespace(code=code, reason="rejection")

    async def factory(url, **kwargs):
        raise original

    with pytest.raises(websocket_runtime.RpcClientError) as raised:
        await websocket_runtime.WebSocketTransport.open(
            "ws://test", socket_factory=factory
        )
    assert isinstance(raised.value, websocket_runtime.RpcConnectError)
    assert raised.value.permanent is permanent
    assert raised.value.status == status
    assert raised.value.code == code
    assert raised.value.__cause__ is original


async def test_served_close_preserves_code_in_requests_notifications_and_closed(
    websocket_runtime,
):
    from fastapi_canon.integrations.websockets import (
        Inject,
        RpcChannel,
        RpcConnection,
        RpcConnectionClose,
        RpcService,
    )
    from fastapi_canon.integrations.websockets.testing import InMemorySocketFactory

    channel = RpcChannel("disconnect")

    @channel.method
    async def fail(connection: Inject[RpcConnection]) -> None:
        await connection.close(
            RpcConnectionClose.INTERNAL_ERROR, reason="server failure"
        )
        await asyncio.Event().wait()

    app = RpcService()
    app.socket("/rpc", channels=[channel])
    async with InMemorySocketFactory(app) as factory:
        transport = await websocket_runtime.WebSocketTransport.open(
            "ws://test/rpc", socket_factory=factory, request_timeout=None
        )
        notifications = transport.notifications()
        listening = asyncio.create_task(anext(notifications))
        with pytest.raises(websocket_runtime.RpcClientError) as raised:
            await transport.request("disconnect.fail")
        assert isinstance(raised.value, websocket_runtime.RpcConnectionLostError)
        assert raised.value.code == 1011
        assert raised.value.reason == "server failure"
        assert raised.value.sent is True
        with pytest.raises(websocket_runtime.RpcConnectionLostError):
            await listening
        assert (await transport.closed).code == 1011
        await notifications.aclose()


async def test_simultaneous_interrupted_sends_keep_their_own_delivery_state(
    websocket_runtime,
):
    from fastapi_canon.integrations.websockets import (
        RpcChannel,
        RpcConnectionClose,
        RpcService,
    )
    from fastapi_canon.integrations.websockets.testing import (
        InMemoryClientSocket,
        InMemorySocketFactory,
    )

    app = RpcService()
    app.socket("/rpc", channels=[RpcChannel("pending")])
    entered = asyncio.Queue()

    class ClientSocket(InMemoryClientSocket):
        async def send(self, message):
            entered.put_nowait(message)
            await super().send(message)

    async with InMemorySocketFactory(app) as factory:
        socket = (await factory("ws://test/rpc")).server_socket
        socket.client_to_server.block()
        transport = websocket_runtime.WebSocketTransport(
            ClientSocket(socket), request_timeout=None
        )
        calls = [
            asyncio.create_task(transport.request(name)) for name in ("one", "two")
        ]
        for _ in calls:
            await entered.get()
        await socket.close(RpcConnectionClose.INTERNAL_ERROR, "interrupted")
        outcomes = await asyncio.gather(*calls, return_exceptions=True)
        assert all(
            isinstance(e, websocket_runtime.RpcConnectionLostError) for e in outcomes
        )
        assert all(e.sent is True and e.code == 1011 for e in outcomes)
        with pytest.raises(websocket_runtime.RpcConnectionLostError) as unsent:
            await transport.request("never sent")
        assert unsent.value.sent is False
        await transport.closed
        assert not transport._pending
        assert not transport._sent


@pytest.mark.parametrize("phase", ["before", "after"])
async def test_application_middleware_errors_retain_identity(runtime, phase):
    original = ValueError("application middleware failed")

    class Middleware:
        @asynccontextmanager
        async def request(self, call):
            if phase == "before":
                raise original
            yield
            raise original

    core = runtime.RpcClientCore(StubTransport(1), middleware=[Middleware()])
    route = runtime.RpcRouteInfo(method="instrumented", result_adapter=TypeAdapter(int))
    with pytest.raises(ValueError) as raised:
        await core.request(route)
    assert raised.value is original
    await core.close()


async def test_header_factory_errors_retain_application_exception_identity(
    websocket_runtime,
):
    original = ValueError("credential refresh failed")

    async def headers():
        raise original

    with pytest.raises(ValueError) as raised:
        await websocket_runtime.GreetingClient.connect(headers=headers)
    assert raised.value is original


async def test_static_transport_close_waits_for_all_and_finalizes_client(
    runtime: ModuleType,
) -> None:
    started, release = asyncio.Event(), asyncio.Event()

    class Failing(StubTransport):
        async def close(self) -> None:
            await started.wait()
            raise OSError("close failed")

    class Blocking(StubTransport):
        async def close(self) -> None:
            started.set()
            await release.wait()
            await super().close()

    blocking = Blocking()
    core = runtime.RpcClientCore({"first": Failing(), "second": blocking})
    closing = asyncio.create_task(core.close())
    await started.wait()
    release.set()
    await closing
    assert blocking.closed == 1
    assert core.closed.done()
    await core.close()
    assert blocking.closed == 1


async def test_static_transport_close_preserves_caller_cancellation(
    runtime: ModuleType,
) -> None:
    started, cancelled = asyncio.Event(), asyncio.Event()

    class Blocking(StubTransport):
        async def close(self) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    core = runtime.RpcClientCore({"first": Blocking(), "second": Blocking()})
    closing = asyncio.create_task(core.close())
    await started.wait()
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert cancelled.is_set()
    assert core.closed.done()
