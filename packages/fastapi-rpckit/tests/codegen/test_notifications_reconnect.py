import asyncio
import json
from collections.abc import AsyncIterator

import pytest
from fastapi_canon.integrations.websockets import RpcChannel, RpcService
from fastapi_canon.integrations.websockets.testing import InMemorySocketFactory
from pydantic import BaseModel, TypeAdapter

from .test_connect import client_module as client_module
from .test_python_runtime import ControlledSocket
from .test_python_runtime import websocket_runtime as websocket_runtime
from .test_reconnect import pool
from .test_reconnect import pool_runtime as pool_runtime


class Notifications:
    def __init__(self):
        self.closed = asyncio.get_running_loop().create_future()
        self.messages = asyncio.Queue()
        self.started = asyncio.Event()
        self.processed = asyncio.Queue()
        self.active = 0

    async def request(self, method, params=None, *, metadata=None):
        return "ok"

    async def notifications(self):
        self.active += 1
        self.started.set()
        try:
            while True:
                item = await self.messages.get()
                if isinstance(item, BaseException):
                    raise item
                yield item
                self.processed.put_nowait(item)
        finally:
            self.active -= 1

    def emit(self, value):
        self.messages.put_nowait(
            {"jsonrpc": "2.0", "method": "update", "params": {"value": value}}
        )

    def drop(self, runtime):
        error = runtime.RpcConnectionLostError(sent=False, code=1012)
        self.closed.set_result(error)
        self.messages.put_nowait(error)

    async def close(self):
        if not self.closed.done():
            self.closed.set_result(None)


async def test_passive_events_resume_against_a_real_served_endpoint(client_module):
    events = asyncio.Queue()
    states = asyncio.Queue()
    channel = RpcChannel("events")

    class Update(BaseModel):
        value: int

    @channel.event(payload=Update)
    async def updates() -> AsyncIterator[Update]:
        while True:
            yield Update(value=await events.get())

    service = RpcService()
    service.socket("/rpc", channels=[channel])
    service.freeze()
    async with InMemorySocketFactory(service) as factory:
        client = await client_module.GreetingClient.connect(
            socket_factory=factory,
            lazy=True,
            reconnect=True,
            request_timeout=None,
            on_state_change=lambda *event: states.put_nowait(event),
        )
        stream = client._rpc.notifications("events.updates", server="primary")
        pending = None
        try:
            async with asyncio.timeout(2):
                pending = asyncio.create_task(anext(stream))
                while not client._rpc._hubs:
                    await asyncio.sleep(0)
                events.put_nowait(1)
                assert (await pending)["params"]["value"] == 1
                await states.get()
                await states.get()
                pending = asyncio.create_task(anext(stream))
                await factory.sockets[0].client_disconnect(1012, "restart")
                assert (await states.get())[1] == "reconnecting"
                assert (await states.get())[1] == "connected"
                while (
                    not client._rpc._hubs
                    or next(iter(client._rpc._hubs.values())).transport.closed.done()
                ):
                    await asyncio.sleep(0)
                events.put_nowait(2)
                assert (await pending)["params"]["value"] == 2
                assert len(client._rpc._hubs) == 1 and factory.attempts == 2
        finally:
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await stream.aclose()
            await client.close()


@pytest.mark.parametrize("typed", [False, True])
async def test_ten_reconnects_share_one_pump_and_retire_old_hubs(pool_runtime, typed):
    before = asyncio.all_tasks()
    created = asyncio.Queue()
    transports, states = [], []

    async def factory(*args, **kwargs):
        transport = Notifications()
        transports.append(transport)
        created.put_nowait(transport)
        return transport

    source = pool(
        pool_runtime,
        factory,
        reconnect=True,
        on_state_change=lambda *event: states.append(event),
    )
    core = pool_runtime.RpcClientCore(source)

    class Envelope(BaseModel):
        jsonrpc: str
        method: str
        params: dict[str, int]

    def stream():
        if not typed:
            return core.notifications("update", server="a")
        return core.subscribe(
            pool_runtime.RpcNotificationInfo(
                method="update",
                server="a",
                message_adapter=TypeAdapter(Envelope),
                payload_adapter=TypeAdapter(dict[str, int]),
            )
        )

    left, right = stream(), stream()
    try:
        async with asyncio.timeout(3):
            for generation in range(11):
                calls = [
                    asyncio.create_task(anext(iterator)) for iterator in (left, right)
                ]
                if generation:
                    transports[-1].drop(pool_runtime)
                transport = await created.get()
                await transport.started.wait()
                # Both consumers register before the one shared pump delivers.
                while len(core._hubs[id(transport)].subscribers) != 2:
                    await asyncio.sleep(0)
                transport.emit(generation)
                for value in await asyncio.gather(*calls):
                    assert (value if typed else value["params"]) == {
                        "value": generation
                    }
                assert transport.active == 1
                assert len(core._hubs) == 1
                assert all(t.active == 0 for t in transports[:-1])
            assert len([event for event in states if event[1] == "reconnecting"]) == 10
            assert not core.closed.done()
            await left.aclose()
            assert len(next(iter(core._hubs.values())).subscribers) == 1
    finally:
        await left.aclose()
        await right.aclose()
        await core.close()
    assert not core._hubs
    assert not asyncio.all_tasks() - before


async def test_overflow_callback_failure_is_reported_once_and_delivery_continues(
    pool_runtime, caplog
):
    transport = Notifications()

    async def factory(*args, **kwargs):
        return transport

    def broken(*event):
        raise ValueError("observer failed")

    core = pool_runtime.RpcClientCore(
        pool(pool_runtime, factory), notification_queue_size=1, on_overflow=broken
    )
    stream = core.notifications("update", server="a")
    first = asyncio.create_task(anext(stream))
    await transport.started.wait()
    transport.emit(0)
    await first
    await transport.processed.get()
    for value in (1, 2, 3, 4):
        transport.emit(value)
    for _ in range(4):
        await transport.processed.get()
    assert core.dropped_notifications == 3
    assert (await anext(stream))["params"]["value"] == 4
    assert (
        len(
            [
                record
                for record in caplog.records
                if record.message == "RPC overflow callback failed"
            ]
        )
        == 1
    )
    await stream.aclose()
    await core.close()


async def test_subscriber_overflow_counts_are_cumulative_and_warn_once(
    pool_runtime, caplog
):
    transport = Notifications()

    async def factory(*args, **kwargs):
        return transport

    source = pool(pool_runtime, factory)
    events = []
    core = pool_runtime.RpcClientCore(
        source,
        notification_queue_size=1,
        on_overflow=lambda *event: events.append(event),
    )
    stream = core.notifications("update", server="a")
    try:
        first = asyncio.create_task(anext(stream))
        await transport.started.wait()
        transport.emit(1)
        assert (await first)["params"]["value"] == 1
        await transport.processed.get()
        for value in (2, 3, 4):
            transport.emit(value)
        for _ in range(3):
            await transport.processed.get()
        assert core.dropped_notifications == 2
        assert events == [("update", 1, "subscriber"), ("update", 2, "subscriber")]
        assert (await anext(stream))["params"]["value"] == 4
        assert (
            len(
                [
                    r
                    for r in caplog.records
                    if "subscriber queue overflowed" in r.message
                ]
            )
            == 1
        )
    finally:
        await stream.aclose()
        await core.close()


async def test_transport_overflow_counts_drops_and_reports_its_buffer(
    websocket_runtime,
):
    socket = ControlledSocket()
    reported, overflowed = [], asyncio.Event()

    def callback(*event):
        reported.append(event)
        if event[1] == 2:
            overflowed.set()

    transport = websocket_runtime.WebSocketTransport(
        socket, notification_queue_size=1, on_overflow=callback
    )
    transport.start_notifications()
    try:
        for value in (1, 2, 3):
            socket.inbox.put_nowait(
                json.dumps(
                    {"jsonrpc": "2.0", "method": "update", "params": {"value": value}}
                )
            )
        await asyncio.wait_for(overflowed.wait(), 1)
        assert transport.dropped_notifications == 2
        assert reported == [(None, 1, "transport"), (None, 2, "transport")]
        notifications = transport.notifications()
        assert (await anext(notifications))["params"]["value"] == 3
        await notifications.aclose()
    finally:
        await transport.close()


@pytest.mark.parametrize("borrowed", [False, True])
async def test_close_wakes_consumers_waiting_for_recovery_without_ending_shared_source(
    pool_runtime, borrowed
):
    before = asyncio.all_tasks()
    first = Notifications()
    opening = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def factory(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return first
        opening.set()
        await release.wait()
        return Notifications()

    source = pool(pool_runtime, factory, reconnect=True)
    core = pool_runtime.RpcClientCore(source, close_transport=not borrowed)
    one, two = (
        core.notifications("update", server="a"),
        core.notifications("update", server="a"),
    )
    a, b = asyncio.create_task(anext(one)), asyncio.create_task(anext(two))
    await first.started.wait()
    first.drop(pool_runtime)
    await opening.wait()
    a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await a
    await one.aclose()
    await core.close()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(b, 1)
    await two.aclose()
    if borrowed:
        assert not source.closed.done()
    await source.close()
    assert not core._hubs
    assert not asyncio.all_tasks() - before


async def test_without_reconnect_notification_failure_is_preserved(pool_runtime):
    transport = Notifications()

    async def factory(*args, **kwargs):
        return transport

    source = pool(pool_runtime, factory)
    core = pool_runtime.RpcClientCore(source)
    stream = core.notifications("update", server="a")
    call = asyncio.create_task(anext(stream))
    await transport.started.wait()
    transport.drop(pool_runtime)
    with pytest.raises(pool_runtime.RpcConnectionLostError):
        await call
    assert not core._hubs
    await stream.aclose()
    await core.close()
