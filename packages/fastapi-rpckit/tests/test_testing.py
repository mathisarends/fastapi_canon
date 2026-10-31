import asyncio

import pytest
from fastapi_canon.integrations.websockets import (
    RpcChannel,
    RpcConnectionClose,
    RpcDisconnect,
    RpcReject,
    RpcRejection,
    RpcService,
)
from fastapi_canon.integrations.websockets.testing import (
    InMemorySocket,
    InMemorySocketFactory,
    RpcTestClient,
    RpcTestHandshakeRejected,
    RpcTestStream,
    SocketFaults,
)

control_channel = RpcChannel("control")


@control_channel.method("ping")
async def ping() -> None:
    return None


service = RpcService()
service.socket("/rooms", channels=(control_channel,), name="rooms")


async def test_requests_reach_the_endpoint() -> None:
    async with RpcTestClient(service, "/rooms") as client:
        assert await client.request("control.ping") is None


async def test_notification_timeout() -> None:
    async with RpcTestClient(service, "/rooms") as client:
        with pytest.raises(TimeoutError):
            await client.next_notification(timeout=0.01)


def test_test_stream_requires_stream_endpoint() -> None:
    with pytest.raises(TypeError, match="not a binary stream"):
        RpcTestStream(service, "/rooms")


def direction(socket, side):
    if side == "client":
        return socket.client_to_server, socket.client.send, socket.receive
    return socket.server_to_client, socket.send, socket.client.recv


@pytest.mark.parametrize("side", ["client", "server"])
@pytest.mark.parametrize("code", [1006, 1008])
async def test_drop_after_frames_preserves_close_information(side, code):
    faults = SocketFaults(drop_after=2, close_code=code, reason="injected drop")
    socket = InMemorySocket(
        "/rpc", **{f"{side}_to_{'server' if side == 'client' else 'client'}": faults}
    )
    _, send, receive = direction(socket, side)
    await send("one")
    await send("two")
    assert await receive() == "one"
    assert await receive() == "two"
    for _ in range(2):
        with pytest.raises(RpcDisconnect) as raised:
            await receive()
        assert raised.value.raw_close_code == code
        assert raised.value.reason == "injected drop"
    assert socket.client.close_code == code
    assert socket.client.close_reason == "injected drop"
    # Abnormal closure is an observation, never a simulated wire close code.
    assert socket.closed is None


@pytest.mark.parametrize("side", ["client", "server"])
async def test_drop_predicate_disconnects_both_views(side):
    socket = InMemorySocket("/rpc")
    faults, send, _ = direction(socket, side)
    faults.drop_when = lambda frame: frame == "drop"
    faults.close_code = 1011
    faults.reason = "predicate"
    with pytest.raises(RpcDisconnect):
        await send("drop")
    for receive in (socket.receive, socket.client.recv):
        with pytest.raises(RpcDisconnect) as raised:
            await receive()
        assert raised.value.raw_close_code == 1011
        assert raised.value.reason == "predicate"


@pytest.mark.parametrize("side", ["client", "server"])
async def test_held_frames_are_released_in_order_without_blocking_send(side):
    socket = InMemorySocket("/rpc")
    faults, send, receive = direction(socket, side)
    faults.hold()
    await send("one")
    await send(b"two")
    await faults.held.wait()
    assert faults.held_count == 2
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0):
            await receive()
    faults.release()
    assert await receive() == "one"
    assert await receive() == b"two"
    assert faults.held_count == 0


@pytest.mark.parametrize("side", ["client", "server"])
async def test_half_open_discards_only_one_direction(side):
    socket = InMemorySocket("/rpc")
    faults, send, receive = direction(socket, side)
    faults.half_open = True
    await send("lost")
    assert socket.disconnect_error is None
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0):
            await receive()
    _, reverse_send, reverse_receive = direction(
        socket, "server" if side == "client" else "client"
    )
    await reverse_send("live")
    assert await reverse_receive() == "live"
    faults.half_open = False
    await send("restored")
    assert await receive() == "restored"


@pytest.mark.parametrize("side", ["client", "server"])
async def test_blocked_send_can_be_cancelled_or_released(side):
    baseline = asyncio.all_tasks()
    socket = InMemorySocket("/rpc")
    faults, send, receive = direction(socket, side)
    faults.block()
    sending = asyncio.create_task(send("cancelled"))
    await faults.blocked.wait()
    sending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sending
    faults.unblock()
    await send("sent")
    assert await receive() == "sent"
    assert asyncio.all_tasks() == baseline


@pytest.mark.parametrize("side", ["client", "server"])
async def test_disconnect_cleans_held_frames_and_wakes_blocked_sends(side):
    baseline = asyncio.all_tasks()
    socket = InMemorySocket("/rpc")
    faults, send, receive = direction(socket, side)
    faults.hold()
    await send("held")
    faults.block()
    sending = asyncio.create_task(send("blocked"))
    await faults.blocked.wait()
    await socket.client_disconnect(1006, "abrupt")
    with pytest.raises(RpcDisconnect):
        await sending
    faults.release()
    assert faults.held_count == 0
    with pytest.raises(RpcDisconnect):
        await receive()
    assert asyncio.all_tasks() == baseline


class ControlledClock:
    def __init__(self):
        self.waiting = asyncio.Queue()

    async def sleep(self, duration):
        ready = asyncio.get_running_loop().create_future()
        self.waiting.put_nowait((duration, ready))
        await ready


@pytest.mark.parametrize("side", ["client", "server"])
async def test_fixed_and_per_frame_delay_use_the_injected_clock_and_cleanup(side):
    baseline = asyncio.all_tasks()
    clock = ControlledClock()
    socket = InMemorySocket("/rpc")
    faults, send, receive = direction(socket, side)
    faults.sleep = clock.sleep
    for delay in (2.0, lambda frame: len(frame) / 2):
        faults.delay = delay
        sending = asyncio.create_task(send("test"))
        duration, ready = await clock.waiting.get()
        assert duration == 2.0
        ready.set_result(None)
        await sending
        assert await receive() == "test"
    sending = asyncio.create_task(send("discard"))
    _, ready = await clock.waiting.get()
    await socket.client_disconnect()
    with pytest.raises(RpcDisconnect):
        await sending
    assert ready.cancelled()
    assert asyncio.all_tasks() == baseline


async def test_server_close_code_and_reason_reach_the_client_adapter():
    socket = InMemorySocket("/rpc")
    await socket.close(RpcConnectionClose.INTERNAL_ERROR, "server failure")
    with pytest.raises(RpcDisconnect) as raised:
        await socket.client.recv()
    assert raised.value.code == RpcConnectionClose.INTERNAL_ERROR
    assert socket.client.close_code == 1011
    assert socket.client.close_reason == "server failure"


async def test_factory_scripts_handshake_rejection_and_repeated_connections():
    baseline = asyncio.all_tasks()
    configured = []

    def configure(socket, attempt):
        configured.append(attempt)
        socket.server_to_client.hold()

    async with InMemorySocketFactory(service, configure=configure) as factory:
        factory.reject_next(status=401, reason="expired")
        factory.reject_next(close_code=1008, reason="policy")
        for expected in (401, 1008):
            with pytest.raises(RpcTestHandshakeRejected) as raised:
                await factory("ws://test/rooms")
            assert (raised.value.status_code or raised.value.close_code) == expected
        for _ in range(2):
            client = await factory(
                "ws://test/rooms", additional_headers={"Authorization": "test"}
            )
            assert client.server_socket.accepted
            assert client.server_socket.handshake.headers["Authorization"] == "test"
            await client.close()
        assert configured == [3, 4]
        assert factory.attempts == 4
    assert all(task.done() for task in factory._tasks)
    assert asyncio.all_tasks() == baseline


async def test_factory_cancellation_during_handshake_leaves_no_tasks():
    baseline = asyncio.all_tasks()
    entered = asyncio.Event()

    async def before_accept(handshake):
        entered.set()
        await asyncio.Event().wait()

    app = RpcService()
    app.socket("/rpc", channels=[control_channel], before_accept=before_accept)
    async with InMemorySocketFactory(app) as factory:
        connecting = asyncio.create_task(factory("ws://test/rpc"))
        await entered.wait()
        connecting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await connecting
    assert asyncio.all_tasks() == baseline


async def test_factory_preserves_actual_server_handshake_rejections():
    async def before_accept(handshake):
        raise RpcReject(RpcRejection.FORBIDDEN, "not allowed")

    app = RpcService()
    app.socket("/rpc", channels=[control_channel], before_accept=before_accept)
    async with InMemorySocketFactory(app) as factory:
        with pytest.raises(RpcTestHandshakeRejected) as raised:
            await factory("ws://test/rpc")
        assert raised.value.close_code == 1008
        assert raised.value.reason == "not allowed"
