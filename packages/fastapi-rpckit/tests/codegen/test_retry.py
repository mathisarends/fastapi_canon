import asyncio
from types import SimpleNamespace

import pytest
from fastapi_canon.integrations.websockets import (
    ProtocolDefinitionError,
    RpcChannel,
    RpcService,
)
from fastapi_canon.integrations.websockets.codegen import (
    generate_python_client,
    generate_typescript_client,
)
from fastapi_canon.integrations.websockets.codegen.ir import build_ir
from fastapi_canon.integrations.websockets.codegen.options import (
    PythonClientOptions,
    TypeScriptClientOptions,
)
from fastapi_canon.integrations.websockets.schema import render_openrpc
from fastapi_canon.integrations.websockets.testing import InMemorySocketFactory
from pydantic import TypeAdapter

from .test_connect import client_module as client_module
from .test_reconnect import Clock, Transport
from .test_reconnect import pool_runtime as pool_runtime


def test_idempotency_round_trips_contract_and_both_generated_metadata(tmp_path):
    channel = RpcChannel("reads")

    @channel.method(idempotent=True)
    async def value() -> int:
        return 1

    @channel.method()
    async def mutate() -> None:
        pass

    document = render_openrpc(channel.protocol, title="Reads")
    assert channel.protocol.method("reads.value").idempotent
    assert document["methods"][0]["x-rpc-idempotent"] is True
    assert "x-rpc-idempotent" not in document["methods"][1]
    ir = build_ir(document)
    assert [route.idempotent for route in ir.operations] == [True, False]
    generate_python_client(
        document, tmp_path / "python", PythonClientOptions(package="reads_client")
    )
    generate_typescript_client(
        document, tmp_path / "typescript", TypeScriptClientOptions()
    )
    assert "idempotent=True" in (tmp_path / "python/routes.py").read_text()
    assert "idempotent: true" in (tmp_path / "typescript/routes.ts").read_text()
    with pytest.raises(ProtocolDefinitionError, match="idempotent"):
        RpcChannel("invalid").method(idempotent=1)


@pytest.mark.parametrize("idempotent", [False, True])
async def test_retry_after_execution_and_lost_response(
    client_module, pool_runtime, idempotent
):
    executions = 0
    channel = RpcChannel("reads")

    @channel.method(idempotent=idempotent)
    async def value() -> int:
        nonlocal executions
        executions += 1
        return 42

    service = RpcService()
    service.socket("/rpc", channels=[channel])

    def configure(socket, attempt):
        if attempt == 1:
            socket.server_to_client.drop_after = 0

    async with (
        InMemorySocketFactory(service, configure=configure) as factory,
        client_module.GreetingClient.connect(
            socket_factory=factory,
            reconnect=True,
            reconnect_random=lambda: 0,
            retry_attempts=1,
            lazy=True,
        ) as client,
    ):
        route = pool_runtime.RpcRouteInfo(
            method="reads.value",
            server="primary",
            result_adapter=TypeAdapter(int),
            idempotent=idempotent,
        )
        if idempotent:
            assert await client._rpc.request(route) == 42
            assert executions == 2
        else:
            with pytest.raises(client_module.RpcConnectionLostError) as raised:
                await client._rpc.request(route)
            assert raised.value.sent
            assert executions == 1


@pytest.mark.parametrize(
    "sent,idempotent,retries", [(False, False, 1), (True, True, 1), (True, False, 0)]
)
async def test_delivery_certainty_and_default_disabled(
    pool_runtime, sent, idempotent, retries
):
    count = 0
    error = pool_runtime.RpcConnectionLostError(sent=sent)

    class Flaky(Transport):
        async def request(self, method, params=None, *, metadata=None):
            nonlocal count
            count += 1
            if count == 1:
                raise error
            return "ok"

    transport = Flaky()
    source = SimpleNamespace(
        get=lambda _: asyncio.sleep(0, transport),
        close=transport.close,
        retry_delay=lambda _: 0,
    )
    core = pool_runtime.RpcClientCore(source, retry_attempts=1)
    route = pool_runtime.RpcRouteInfo(
        method="work", result_adapter=TypeAdapter(str), idempotent=idempotent
    )
    if retries:
        assert await core.request(route) == "ok"
    else:
        with pytest.raises(pool_runtime.RpcConnectionLostError) as raised:
            await core.request(route)
        assert raised.value is error
    assert count == 1 + retries
    await core.close()
    count = 0
    disabled = pool_runtime.RpcClientCore(source)
    with pytest.raises(pool_runtime.RpcConnectionLostError):
        await disabled.request(route)
    assert count == 1
    await disabled.close()


@pytest.mark.parametrize("finish", ["deadline", "cancel", "close"])
async def test_retry_backoff_stays_within_logical_deadline_and_cleans_up(
    pool_runtime, monkeypatch, finish
):
    before = asyncio.all_tasks()
    clock = Clock(monkeypatch, pool_runtime)
    delayed = asyncio.Event()
    calls = 0

    class Flaky(Transport):
        async def request(self, method, params=None, *, metadata=None):
            nonlocal calls
            calls += 1
            raise pool_runtime.RpcConnectionLostError(sent=False)

    transport = Flaky()

    async def acquire(_):
        return transport

    def delay(_):
        delayed.set()
        return 100

    source = SimpleNamespace(
        get=acquire, close=transport.close, request_timeout=5, retry_delay=delay
    )
    core = pool_runtime.RpcClientCore(source, retry_attempts=3)
    route = pool_runtime.RpcRouteInfo(method="work", result_adapter=TypeAdapter(str))
    task = asyncio.create_task(core.request(route))
    _, total = await clock.timers.get()
    await delayed.wait()
    if finish == "deadline":
        total.reschedule(asyncio.get_running_loop().time())
        with pytest.raises(pool_runtime.RpcRequestTimeoutError):
            await task
    elif finish == "cancel":
        task.cancel("stop retry")
        with pytest.raises(asyncio.CancelledError, match="stop retry"):
            await task
    else:
        await core.close()
        with pytest.raises(pool_runtime.RpcConnectionLostError):
            await task
    assert calls == 1
    await core.close()
    assert not asyncio.all_tasks() - before


async def test_per_attempt_timeout_can_retry_without_resetting_total_budget(
    pool_runtime, monkeypatch
):
    clock = Clock(monkeypatch, pool_runtime)
    sent = asyncio.Event()
    attempts = 0

    class SlowFirst(Transport):
        async def request(self, method, params=None, *, metadata=None):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                sent.set()
                await asyncio.Future()
            return "ok"

    transport = SlowFirst()

    async def acquire(_):
        return transport

    source = SimpleNamespace(
        get=acquire, close=transport.close, request_timeout=5, retry_delay=lambda _: 0
    )
    core = pool_runtime.RpcClientCore(source, retry_attempts=1, retry_attempt_timeout=1)
    route = pool_runtime.RpcRouteInfo(
        method="read", result_adapter=TypeAdapter(str), idempotent=True
    )
    task = asyncio.create_task(core.request(route))
    _, total = await clock.timers.get()
    _, first = await clock.timers.get()
    await sent.wait()
    first.reschedule(asyncio.get_running_loop().time())
    assert await task == "ok"
    assert attempts == 2
    assert not total.expired()
    assert clock.timers.qsize() == 1  # Second attempt, no replacement overall timer.
    await core.close()
