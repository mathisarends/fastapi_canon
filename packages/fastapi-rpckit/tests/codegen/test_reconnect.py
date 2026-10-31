import asyncio
import importlib
from types import SimpleNamespace

import pytest
from fastapi_canon.integrations.websockets import RpcChannel, RpcService
from fastapi_canon.integrations.websockets.testing import InMemorySocketFactory
from pydantic import BaseModel, TypeAdapter

from .test_connect import client_module as client_module


@pytest.fixture
def pool_runtime(client_module):
    return importlib.import_module(f"{client_module.__name__}.internal")


class Transport:
    def __init__(self):
        self.closed = asyncio.get_running_loop().create_future()

    async def request(self, method, params=None, *, metadata=None):
        return "ok"

    async def notifications(self):
        if False:
            yield {}

    async def close(self):
        if not self.closed.done():
            self.closed.set_result(None)


class Clock:
    def __init__(self, monkeypatch, runtime):
        self.sleeps = asyncio.Queue()
        self.timers = asyncio.Queue()
        proxy = SimpleNamespace(**vars(asyncio))
        proxy.sleep = self.sleep
        proxy.timeout = self.timeout
        for suffix in ("connection", "core"):
            module = importlib.import_module(f"{runtime.__name__}.{suffix}")
            monkeypatch.setattr(module, "asyncio", proxy)

    async def sleep(self, delay):
        release = asyncio.Event()
        self.sleeps.put_nowait((delay, release))
        await release.wait()

    def timeout(self, delay):
        waiting = asyncio.timeout(None)
        if delay is not None:
            self.timers.put_nowait((delay, waiting))
        return waiting


def pool(runtime, factory, **options):
    return runtime.RpcTransportPool(
        endpoints=[SimpleNamespace(server="a", url="ws://test", subprotocols=())],
        transport_factory=factory,
        request_timeout=None,
        **options,
    )


async def test_shared_connect_attempt_survives_one_waiter_cancellation(pool_runtime):
    before = asyncio.all_tasks()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0
    transport = Transport()

    async def factory(*args, **kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return transport

    source = pool(pool_runtime, factory)
    first = asyncio.create_task(source.get("a"))
    second = asyncio.create_task(source.get("a"))
    await entered.wait()
    first.cancel("one caller cancelled")
    with pytest.raises(asyncio.CancelledError, match="one caller cancelled"):
        await first
    assert not second.done()
    release.set()
    assert await second is transport
    assert calls == 1
    assert source.state == "connected"
    await source.close()
    assert not source._connecting
    assert not asyncio.all_tasks() - before


@pytest.mark.parametrize("phase", ["connect", "backoff"])
async def test_close_interrupts_connect_or_backoff_without_later_state_changes(
    pool_runtime, monkeypatch, phase
):
    before = asyncio.all_tasks()
    clock = Clock(monkeypatch, pool_runtime)
    entered = asyncio.Event()
    exited = asyncio.Event()
    states = []
    count = 0
    transport = Transport()

    async def factory(*args, **kwargs):
        nonlocal count
        count += 1
        if phase == "backoff" and count == 1:
            return transport
        entered.set()
        if phase == "backoff":
            raise pool_runtime.RpcConnectError("unavailable")
        try:
            await asyncio.Future()
        finally:
            exited.set()

    source = pool(
        pool_runtime,
        factory,
        reconnect=True,
        on_state_change=lambda *event: states.append(event),
    )
    if phase == "backoff":
        await source.get("a")
        transport.closed.set_result(
            pool_runtime.RpcConnectionLostError(sent=False, code=1012)
        )
        await clock.sleeps.get()
    waiting = asyncio.create_task(source.get("a"))
    await entered.wait()
    await source.close()
    with pytest.raises(pool_runtime.RpcConnectionLostError):
        await waiting
    assert source.state == "closed" and await source.closed is None
    assert states[-1][1] == "closed"
    assert not source._connecting
    if phase == "connect":
        assert exited.is_set()
        assert "connected" not in [s[1] for s in states]
    assert not asyncio.all_tasks() - before


async def test_full_jitter_caps_delay_and_exhaustion_preserves_last_error(
    pool_runtime, monkeypatch
):
    before = asyncio.all_tasks()
    clock = Clock(monkeypatch, pool_runtime)
    states = []
    errors = []
    transport = Transport()
    calls = 0

    async def factory(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return transport
        error = pool_runtime.RpcConnectError(f"attempt {calls}")
        errors.append(error)
        raise error

    source = pool(
        pool_runtime,
        factory,
        reconnect=True,
        reconnect_max_attempts=4,
        reconnect_initial_delay=0.25,
        reconnect_max_delay=0.5,
        reconnect_random=lambda: 0.5,
        on_state_change=lambda *event: states.append(event),
    )
    await source.get("a")
    transport.closed.set_result(
        pool_runtime.RpcConnectionLostError(sent=False, code=1001)
    )
    for expected in (0.125, 0.25, 0.25):
        delay, release = await asyncio.wait_for(clock.sleeps.get(), 1)
        assert delay == expected
        assert not source.closed.done()
        release.set()
    last = await asyncio.wait_for(source.closed, 1)
    assert last is errors[-1]
    assert calls == 5
    with pytest.raises(pool_runtime.RpcConnectError) as raised:
        await source.get("a")
    assert raised.value is last
    assert [s[1] for s in states] == [
        "connecting",
        "connected",
        "reconnecting",
        "closed",
    ]
    await source.close()
    assert not asyncio.all_tasks() - before


@pytest.mark.parametrize("permanent", [True, False])
async def test_initial_failure_is_terminal_and_never_retried(pool_runtime, permanent):
    error = pool_runtime.RpcConnectError("initial failure", permanent=permanent)
    calls = 0

    async def factory(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise error

    source = pool(pool_runtime, factory, reconnect=True)
    with pytest.raises(pool_runtime.RpcConnectError) as raised:
        await source.get("a")
    assert raised.value is error
    assert await source.closed is error
    assert calls == 1 and source.state == "closed"
    await source.close()


async def test_eager_failure_interrupts_other_openers(pool_runtime):
    error = pool_runtime.RpcConnectError("unauthorized", permanent=True, status=401)
    both, exited = asyncio.Event(), asyncio.Event()

    async def factory(url, **kwargs):
        if url == "a":
            await both.wait()
            raise error
        both.set()
        try:
            await asyncio.Future()
        finally:
            exited.set()

    source = pool_runtime.RpcTransportPool(
        endpoints=[
            SimpleNamespace(server=name, url=name, subprotocols=())
            for name in ("a", "b")
        ],
        transport_factory=factory,
        request_timeout=None,
    )
    with pytest.raises(pool_runtime.RpcConnectError) as raised:
        await asyncio.wait_for(source.open_all(), 1)
    assert raised.value is error and await source.closed is error
    assert exited.is_set() and not source._connecting


async def test_classifier_override_and_callback_failures_preserve_cleanup(
    pool_runtime, monkeypatch, caplog
):
    clock = Clock(monkeypatch, pool_runtime)
    transport = Transport()
    calls = 0

    async def factory(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise pool_runtime.RpcConnectError("temporary credentials", permanent=True)
        return transport if calls == 1 else Transport()

    def broken_callback(*args):
        raise ValueError("callback failed")

    source = pool(
        pool_runtime,
        factory,
        reconnect=True,
        reconnect_should_retry=lambda error: True,
        reconnect_random=lambda: 0,
        on_state_change=broken_callback,
    )
    await source.get("a")
    transport.closed.set_result(
        pool_runtime.RpcConnectionLostError(
            sent=False, code=1008, reason="credential_expired"
        )
    )
    delay, release = await asyncio.wait_for(clock.sleeps.get(), 1)
    assert delay == 0
    release.set()
    assert await source.get("a") is not transport
    assert source.state == "connected" and not source.closed.done()
    await source.close()
    assert await source.closed is None and not source._connecting
    assert "RPC state callback failed" in caplog.text


async def test_zero_reconnect_budget_stops_without_an_attempt(pool_runtime):
    transport = Transport()
    calls = 0

    async def factory(*args, **kwargs):
        nonlocal calls
        calls += 1
        return transport

    source = pool(pool_runtime, factory, reconnect=True, reconnect_max_attempts=0)
    await source.get("a")
    error = pool_runtime.RpcConnectionLostError(sent=False, code=1001)
    transport.closed.set_result(error)
    assert await source.closed is error and calls == 1
    await source.close()


async def test_borrowed_source_stays_open_while_local_client_closed_resolves(
    pool_runtime,
):
    async def factory(*args, **kwargs):
        return Transport()

    source = pool(pool_runtime, factory)
    await source.get("a")
    core = pool_runtime.RpcClientCore(source, close_transport=False)
    closed = core.closed
    await core.close()
    assert await closed is None
    assert core.state == "closed"
    assert source.state == "connected" and not source.closed.done()
    await source.close()


async def test_terminal_server_does_not_replace_other_server_transport(pool_runtime):
    transports = {name: Transport() for name in ("a", "b")}
    calls = []

    async def factory(url, **kwargs):
        calls.append(url)
        return transports[url]

    source = pool_runtime.RpcTransportPool(
        endpoints=[
            SimpleNamespace(server=name, url=name, subprotocols=())
            for name in transports
        ],
        transport_factory=factory,
        reconnect=True,
        request_timeout=None,
    )
    await source.open_all()
    error = pool_runtime.RpcConnectionLostError(
        sent=False, code=1008, reason="forbidden"
    )
    transports["a"].closed.set_result(error)
    assert await source.closed is error
    assert source.states["a"] == "closed" and source.states["b"] == "connected"
    assert await source.get("b") is transports["b"]
    assert sorted(calls) == ["a", "b"]
    with pytest.raises(TypeError):
        source.states["b"] = "closed"
    await source.close()


async def test_request_deadline_bounds_shared_connection_wait_and_wire_work(
    pool_runtime, monkeypatch
):
    clock = Clock(monkeypatch, pool_runtime)
    entered, release, sent, cancelled = (asyncio.Event() for _ in range(4))

    class SlowTransport(Transport):
        async def request(self, method, params=None, *, metadata=None):
            sent.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    async def factory(*args, **kwargs):
        entered.set()
        await release.wait()
        return SlowTransport()

    source = pool_runtime.RpcTransportPool(
        endpoints=[SimpleNamespace(server="a", url="a", subprotocols=())],
        transport_factory=factory,
        request_timeout=5,
    )
    core = pool_runtime.RpcClientCore(source)
    route = pool_runtime.RpcRouteInfo(
        method="work", server="a", result_adapter=TypeAdapter(str)
    )
    task = asyncio.create_task(core.request(route))
    await entered.wait()
    duration, total = await clock.timers.get()
    assert duration == 5
    _, attempt = await clock.timers.get()
    release.set()
    await sent.wait()
    total.reschedule(asyncio.get_running_loop().time())
    with pytest.raises(pool_runtime.RpcRequestTimeoutError):
        await task
    assert cancelled.is_set()
    with pytest.raises(RuntimeError, match="finished"):
        attempt.reschedule(asyncio.get_running_loop().time())
    await core.close()


@pytest.mark.parametrize("reject", [False, True])
@pytest.mark.parametrize("close", [(1012, "restart"), (1008, "credentials_expired")])
async def test_proactive_recovery_refreshes_headers_or_surfaces_permanent_rejection(
    client_module,
    reject,
    close,
):
    channel = RpcChannel("greeting")

    class Params(BaseModel):
        name: str

    @channel.method()
    async def say(params: Params) -> dict[str, str]:
        return {"text": params.name}

    service = RpcService()
    service.socket("/rpc", channels=[channel])
    states = asyncio.Queue()
    header_calls = 0

    async def headers():
        nonlocal header_calls
        header_calls += 1
        return {"Authorization": f"token {header_calls}"}

    async with InMemorySocketFactory(service) as factory:
        client = await client_module.GreetingClient.connect(
            socket_factory=factory,
            lazy=True,
            reconnect=True,
            request_timeout=None,
            headers=headers,
            on_state_change=lambda *event: states.put_nowait(event),
        )
        try:
            assert (await client.greeting.say(name="first")).text == "first"
            assert (await states.get())[1] == "connecting"
            assert (await states.get())[1] == "connected"
            if reject:
                factory.reject_next(status=401)
            await factory.sockets[0].client_disconnect(*close)
            server, state, error = await asyncio.wait_for(states.get(), 1)
            assert server == client_module.ServerName.PRIMARY
            assert state == "reconnecting" and error.code == close[0]
            if reject:
                error = await asyncio.wait_for(client.closed, 1)
                assert isinstance(error, client_module.RpcConnectError)
                assert error.permanent and error.status == 401
                assert (await states.get())[1] == "closed"
                assert client.state == "closed"
            else:
                assert (await asyncio.wait_for(states.get(), 1))[1] == "connected"
                assert not client.closed.done()
                assert (await client.greeting.say(name="second")).text == "second"
                assert (
                    factory.sockets[1].handshake.headers["Authorization"] == "token 2"
                )
            assert factory.attempts == 2 and header_calls == 2
        finally:
            await client.close()
