import asyncio
import json
import socket as sockets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi_canon.integrations.websockets import (
    Inject,
    RpcChannel,
    RpcConnectionClose,
    RpcDisconnect,
    RpcDrain,
    RpcMiddleware,
    RpcService,
    RpcSocket,
)
from fastapi_canon.integrations.websockets.adapters.fastapi import create_router
from fastapi_canon.integrations.websockets.testing import (
    InMemorySocketFactory,
    RpcTestHandshakeRejected,
)
from pydantic import BaseModel
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus


def request(request_id, method="work.slow"):
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method})


def slow_service(*, middleware=()):
    entered, release, cancelled = (asyncio.Event() for _ in range(3))
    channel = RpcChannel("work")

    @channel.method()
    async def slow() -> int:
        entered.set()
        try:
            await release.wait()
            return 42
        finally:
            cancelled.set()

    service = RpcService(middleware=middleware, strict_errors=True)
    service.socket("/rpc", channels=[channel])
    return service, entered, release, cancelled


async def test_drain_delivers_response_rejects_new_work_and_reports_restart():
    before = asyncio.all_tasks()
    observed, scopes = [], []

    class Instrumentation(RpcMiddleware):
        @asynccontextmanager
        async def connection(self, scope):
            yield
            if scope.connection is not None:
                observed.append(scope.connection)

    class Middleware(RpcMiddleware):
        @asynccontextmanager
        async def connection(self, scope):
            yield
            scopes.append(scope)

    service, entered, release, _ = slow_service(
        middleware=[Instrumentation(), Middleware()]
    )
    async with InMemorySocketFactory(service) as factory:
        client = await factory("ws://test/rpc")
        await client.send(request(1))
        await entered.wait()
        draining = asyncio.create_task(service.drain.drain(timeout=2))
        await asyncio.sleep(0)
        assert service.drain.draining
        await client.send(request(2))
        response = json.loads(await client.recv())
        assert response["id"] == 2
        assert response["error"]["data"]["code"] == "server_shutting_down"
        with pytest.raises(RpcTestHandshakeRejected) as denied:
            await factory("ws://test/rpc")
        assert denied.value.close_code == 1013
        release.set()
        assert json.loads(await client.recv()) == {
            "jsonrpc": "2.0",
            "id": 1,
            "result": 42,
        }
        await draining
        with pytest.raises(RpcDisconnect):
            await client.recv()
        assert client.close_code == 1012
        assert service.drain.connection_count == 0
        await service.drain.drain(timeout=0)  # Idempotent, first caller's budget wins.
    assert observed[0].close_code == RpcConnectionClose.SERVICE_RESTART
    accepted = next(scope for scope in scopes if scope.connection is not None)
    assert accepted.connection.close_code == RpcConnectionClose.SERVICE_RESTART
    assert not asyncio.all_tasks() - before


async def test_controlled_grace_expiry_cancels_stuck_work_and_cleans_sessions(
    monkeypatch,
):
    import fastapi_canon.integrations.websockets.drain as module

    before = asyncio.all_tasks()
    expired, timer_started = asyncio.Event(), asyncio.Event()
    proxy = SimpleNamespace(**vars(asyncio))
    real_wait = asyncio.wait
    calls = []

    async def wait(work, *, timeout):
        calls.append(timeout)
        if len(calls) == 1:
            timer_started.set()
            await expired.wait()
            return set(), set(work)
        return await real_wait(work, timeout=timeout)

    proxy.wait = wait
    monkeypatch.setattr(module, "asyncio", proxy)
    service, entered, _, cancelled = slow_service()
    async with InMemorySocketFactory(service) as factory:
        client = await factory("ws://test/rpc")
        await client.send(request(1))
        await entered.wait()
        first = asyncio.create_task(service.drain.drain(timeout=10))
        await timer_started.wait()
        second = asyncio.create_task(service.drain.drain(timeout=1))
        first.cancel("caller stopped waiting")
        with pytest.raises(asyncio.CancelledError, match="caller stopped"):
            await first
        expired.set()
        await second
        assert cancelled.is_set()
        assert client.close_code == 1012
        assert calls[0] == 9
        assert calls[1] <= 10
        assert service.drain.connection_count == 0
    assert not asyncio.all_tasks() - before


async def test_drain_stops_event_producer_and_flushes_queued_response():
    producer_started, stopped, send_started, allow_send = (
        asyncio.Event() for _ in range(4)
    )
    channel = RpcChannel("work")

    class Update(BaseModel):
        value: int

    @channel.event()
    async def updates() -> AsyncIterator[Update]:
        producer_started.set()
        try:
            await asyncio.Future()
            yield Update(value=1)
        finally:
            stopped.set()

    @channel.method()
    async def quick() -> int:
        return 42

    service = RpcService()
    service.socket("/rpc", channels=[channel])

    def configure(socket, _):
        original = socket.send

        async def send(message):
            send_started.set()
            await allow_send.wait()
            await original(message)

        socket.send = send

    async with InMemorySocketFactory(service, configure=configure) as factory:
        client = await factory("ws://test/rpc")
        await producer_started.wait()
        await client.send(request(1, "work.quick"))
        await send_started.wait()
        draining = asyncio.create_task(service.drain.drain(timeout=2))
        await stopped.wait()
        assert not draining.done()
        allow_send.set()
        assert json.loads(await client.recv())["result"] == 42
        await draining
        assert client.close_code == 1012


@pytest.mark.parametrize("drain", [False, True])
async def test_shutdown_cleans_subscription_while_another_request_is_stuck(drain):
    before = asyncio.all_tasks()
    subscribed, stopped, working = (asyncio.Event() for _ in range(3))
    channel = RpcChannel("work")

    @channel.subscription()
    async def events() -> AsyncIterator[int]:
        subscribed.set()
        try:
            await asyncio.Future()
            yield 1
        finally:
            stopped.set()

    @channel.method()
    async def slow() -> None:
        working.set()
        await asyncio.Future()

    service = RpcService()
    service.socket("/rpc", channels=[channel])
    async with InMemorySocketFactory(service) as factory:
        client = await factory("ws://test/rpc")
        await client.send(request(1, "work.events.subscribe"))
        await asyncio.wait_for(subscribed.wait(), 1)
        await client.recv()
        await client.send(request(2))
        await asyncio.wait_for(working.wait(), 1)
        if drain:
            await service.drain.drain(timeout=0.2)
        else:
            factory._tasks[0].cancel()
        await asyncio.gather(*factory._tasks, return_exceptions=True)
        assert stopped.is_set()
        assert service.drain.connection_count == 0
    assert not asyncio.all_tasks() - before


async def test_blocked_flush_is_cancelled_at_grace_expiry(monkeypatch):
    import fastapi_canon.integrations.websockets.drain as module

    send_started, send_cancelled, expired, timer_started = (
        asyncio.Event() for _ in range(4)
    )
    channel = RpcChannel("work")

    @channel.method()
    async def quick() -> int:
        return 42

    service = RpcService()
    service.socket("/rpc", channels=[channel])
    proxy = SimpleNamespace(**vars(asyncio))
    real_wait = asyncio.wait
    calls = 0

    async def wait(work, *, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            timer_started.set()
            await expired.wait()
            return set(), set(work)
        return await real_wait(work, timeout=timeout)

    proxy.wait = wait
    monkeypatch.setattr(module, "asyncio", proxy)

    def configure(socket, _):
        async def send(message):
            send_started.set()
            try:
                await asyncio.Future()
            finally:
                send_cancelled.set()

        socket.send = send

    async with InMemorySocketFactory(service, configure=configure) as factory:
        client = await factory("ws://test/rpc")
        await client.send(request(1, "work.quick"))
        await send_started.wait()
        draining = asyncio.create_task(service.drain.drain(timeout=2))
        await timer_started.wait()
        expired.set()
        await draining
        assert send_cancelled.is_set()
        assert client.close_code == 1012
        assert service.drain.connection_count == 0


async def test_drain_rechecks_handshake_after_authentication_race():
    entered, release = asyncio.Event(), asyncio.Event()
    service = RpcService()

    async def authenticate(handshake):
        entered.set()
        await release.wait()

    service.socket("/rpc", channels=[RpcChannel("auth")], before_accept=authenticate)
    async with InMemorySocketFactory(service) as factory:
        opening = asyncio.create_task(factory("ws://test/rpc"))
        await entered.wait()
        draining = asyncio.create_task(service.drain.drain(timeout=2))
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(RpcTestHandshakeRejected) as denied:
            await opening
        await draining
        assert denied.value.close_code == 1013
        assert service.drain.connection_count == 0


async def test_disconnect_during_drain_preserves_peer_close_cause():
    service, entered, _, cancelled = slow_service()
    async with InMemorySocketFactory(service) as factory:
        client = await factory("ws://test/rpc")
        await client.send(request(1))
        await entered.wait()
        draining = asyncio.create_task(service.drain.drain(timeout=2))
        await asyncio.sleep(0)
        await factory.sockets[0].client_disconnect(1001, "Going away")
        await draining
        assert cancelled.is_set()
        assert client.close_code == 1001
        assert client.close_reason == "Going away"
        assert service.drain.connection_count == 0


@pytest.mark.parametrize("kind", ["binary", "raw"])
async def test_long_lived_streams_close_with_restart_at_grace_expiry(kind, monkeypatch):
    import fastapi_canon.integrations.websockets.drain as module

    entered, cancelled, expired, timer_started = (asyncio.Event() for _ in range(4))
    channel = RpcChannel("streams")

    @channel.stream()
    async def frames() -> AsyncIterator[bytes]:
        entered.set()
        try:
            await asyncio.Future()
            yield b"frame"
        finally:
            cancelled.set()

    @channel.socket()
    async def raw(socket: Inject[RpcSocket]) -> None:
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    service = RpcService()
    if kind == "binary":
        service.stream("/stream", frames)
    else:
        service.socket("/stream", raw)
    proxy = SimpleNamespace(**vars(asyncio))
    real_wait = asyncio.wait
    calls = 0

    async def wait(work, *, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            timer_started.set()
            await expired.wait()
            return set(), set(work)
        return await real_wait(work, timeout=timeout)

    proxy.wait = wait
    monkeypatch.setattr(module, "asyncio", proxy)
    async with InMemorySocketFactory(service) as factory:
        client = await factory("ws://test/stream")
        await entered.wait()
        draining = asyncio.create_task(service.drain.drain(timeout=2))
        await timer_started.wait()
        expired.set()
        await draining
        assert cancelled.is_set()
        assert client.close_code == 1012
        assert service.drain.connection_count == 0


async def test_pre_shutdown_drain_with_real_uvicorn_and_lifespan_cleanup():
    service, entered, release, _ = slow_service()
    drain_started, lifespan_finished, ready = (asyncio.Event() for _ in range(3))

    @asynccontextmanager
    async def lifespan(app):
        yield
        await service.drain.drain(timeout=0)
        lifespan_finished.set()

    app = FastAPI(lifespan=lifespan)
    app.include_router(create_router(service))

    @app.post("/drain")
    async def drain():
        drain_started.set()
        await service.drain.drain(timeout=2)
        return {"drained": True}

    class Server(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            ready.set()

    with sockets.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = Server(
            uvicorn.Config(
                app,
                ws="websockets-sansio",
                log_level="error",
                timeout_graceful_shutdown=2,
            )
        )
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            await asyncio.wait_for(ready.wait(), 5)
            async with (
                connect(f"ws://127.0.0.1:{port}/rpc") as client,
                httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http,
            ):
                await client.send(request(1))
                await entered.wait()
                draining = asyncio.create_task(http.post("/drain"))
                await drain_started.wait()
                with pytest.raises(InvalidStatus) as denied:
                    await connect(f"ws://127.0.0.1:{port}/rpc")
                assert denied.value.response.status_code == 503
                release.set()
                assert json.loads(await client.recv())["result"] == 42
                with pytest.raises(ConnectionClosed) as closed:
                    await client.recv()
                assert closed.value.rcvd.code == 1012
                assert (await draining).json() == {"drained": True}
        finally:
            release.set()
            server.should_exit = True
            await asyncio.wait_for(serving, 5)
        assert lifespan_finished.is_set()
        assert service.drain.connection_count == 0


@pytest.mark.parametrize("timeout", [-1, float("inf"), float("nan"), True])
async def test_drain_rejects_invalid_timeout(timeout):
    with pytest.raises(ValueError, match="timeout"):
        await RpcDrain().drain(timeout=timeout)
