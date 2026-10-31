import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

import pytest
from fastapi_canon.integrations.websockets import (
    Inject,
    ProtocolDefinitionError,
    RpcChannel,
    RpcConnection,
    RpcConnectionClose,
    RpcDisconnect,
    RpcErrorBinding,
    RpcErrorCode,
    RpcHandshake,
    RpcLimits,
    RpcMiddleware,
    RpcModel,
    RpcRejection,
    RpcRequestCancelledError,
    RpcRequestTimeoutError,
    RpcService,
)

from .deadlines import ControlledDeadlines
from .testing import InMemorySocket, RpcTestClient


class DeadlineRecorder(RpcMiddleware):
    def __init__(self):
        self.started = asyncio.Queue()
        self.finished = asyncio.Queue()

    @asynccontextmanager
    async def request(self, scope):
        self.started.put_nowait(scope)
        try:
            yield
        finally:
            self.finished.put_nowait(scope)


def deadline_request(request_id, method="deadlines.work"):
    return {"jsonrpc": "2.0", "id": request_id, "method": method}


def cancel_notification(request_id):
    return {"jsonrpc": "2.0", "method": "$/cancelRequest", "params": {"id": request_id}}


@asynccontextmanager
async def deadline_socket(app, socket=None):
    socket = socket or InMemorySocket("/rpc")
    serving = asyncio.create_task(app.serve(socket))
    try:
        async with asyncio.timeout(2):
            yield socket, serving
    finally:
        await socket.client_disconnect()
        await asyncio.wait_for(serving, 1)


async def test_hung_calls_expire_including_queued_batch_item_and_release_capacity(
    monkeypatch,
):
    clock = ControlledDeadlines(monkeypatch)
    entered = asyncio.Queue()
    finished = []
    recorder = DeadlineRecorder()
    channel = RpcChannel("deadlines")

    @channel.method()
    async def work() -> None:
        entered.put_nowait(True)
        try:
            await asyncio.Future()
        finally:
            finished.append(True)

    @channel.method(timeout=None)
    async def ping() -> str:
        return "pong"

    app = RpcService(
        limits=RpcLimits(max_concurrency=2, max_pending_requests=3, request_timeout=5),
        strict_errors=True,
        middleware=[recorder],
    )
    app.socket("/rpc", channels=[channel])
    async with deadline_socket(app) as (socket, _):
        await socket.client_send(json.dumps([deadline_request(i) for i in (1, 2, 3)]))
        timers = [await clock.next() for _ in range(3)]
        await entered.get()
        await entered.get()
        # Expire the queued item first so it never enters the handler.
        for _, waiting in reversed(timers):
            clock.expire(waiting)
        response = json.loads(await socket.client_receive())
        assert [r["id"] for r in response] == [1, 2, 3]
        assert all(r["error"]["data"]["code"] == "request_timeout" for r in response)
        assert all(r["error"]["code"] == RpcErrorCode.SERVER_ERROR for r in response)
        assert finished == [True, True]
        assert entered.empty()
        for _ in range(3):
            scope = await recorder.finished.get()
            assert isinstance(scope.outcome.error, RpcRequestTimeoutError)
            assert not scope.outcome.cancelled
        await socket.client_send(json.dumps(deadline_request(4, "deadlines.ping")))
        assert json.loads(await socket.client_receive())["result"] == "pong"


async def test_cancel_bypasses_saturated_slots_preserves_id_types_and_cleans_scopes():
    entered = asyncio.Event()
    stopped = asyncio.Event()
    exits = []
    recorder = DeadlineRecorder()

    class Resolver:
        @asynccontextmanager
        async def enter_scope(self):
            try:
                yield self
            finally:
                exits.append(True)

        async def resolve(self, dependency):
            return "dependency"

    channel = RpcChannel("deadlines")

    @channel.method()
    async def work(value: Inject[str]) -> None:
        assert value == "dependency"
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            stopped.set()
            raise

    @channel.method()
    async def ping() -> str:
        return "pong"

    app = RpcService(
        limits=RpcLimits(max_concurrency=1, max_pending_requests=2),
        strict_errors=True,
        middleware=[recorder],
    )
    app.socket("/rpc", channels=[channel])
    socket = InMemorySocket("/rpc")
    serving = asyncio.create_task(app.serve(socket, resolver=Resolver()))
    try:
        async with asyncio.timeout(2):
            await socket.client_send(json.dumps(deadline_request(1)))
            await entered.wait()
            await socket.client_send(json.dumps(deadline_request("1")))
            await recorder.started.get()
            await recorder.started.get()  # String id is queued behind integer id.
            await socket.client_send(json.dumps(cancel_notification("unknown")))
            await socket.client_send(json.dumps(deadline_request(9, "deadlines.ping")))
            rejection = json.loads(await socket.client_receive())
            assert rejection["error"]["data"]["code"] == "pending_limit"
            await socket.client_send(json.dumps(cancel_notification("1")))
            queued = json.loads(await socket.client_receive())
            assert queued["id"] == "1"
            assert queued["error"]["code"] == -32800
            assert queued["error"]["data"]["code"] == "request_cancelled"
            assert not stopped.is_set()
            await socket.client_send(json.dumps(cancel_notification(1)))
            running = json.loads(await socket.client_receive())
            assert running["id"] == 1 and type(running["id"]) is int
            assert running["error"]["data"]["code"] == "request_cancelled"
            assert stopped.is_set()
            assert exits == [
                True
            ]  # Queued invocation never entered a dependency scope.
            scopes = [await recorder.finished.get() for _ in range(3)]
            cancelled = [
                s
                for s in scopes
                if isinstance(s.outcome.error, RpcRequestCancelledError)
            ]
            assert len(cancelled) == 2
            assert all(s.outcome.cancelled for s in cancelled)
            await socket.client_send(json.dumps(cancel_notification(1)))  # Finished id.
            await socket.client_send(json.dumps(deadline_request(10, "deadlines.ping")))
            assert json.loads(await socket.client_receive())["result"] == "pong"
    finally:
        await socket.client_disconnect()
        await asyncio.wait_for(serving, 1)


async def test_same_batch_cancel_before_task_start_keeps_one_batch_response():
    channel = RpcChannel("deadlines")

    @channel.method()
    async def work() -> None:
        pytest.fail("Cancelled before the invocation starts")

    @channel.method()
    async def ping() -> str:
        return "pong"

    app = RpcService(limits=RpcLimits(max_pending_requests=2))
    app.socket("/rpc", channels=[channel])
    async with deadline_socket(app) as (socket, _):
        await socket.client_send(
            json.dumps(
                [
                    deadline_request(1),
                    cancel_notification(1),
                    deadline_request(2, "deadlines.ping"),
                    {**cancel_notification(2), "id": 3},
                    cancel_notification("unknown"),
                ]
            )
        )
        response = json.loads(await socket.client_receive())
        assert [r["id"] for r in response] == [1, 2, 3]
        assert response[0]["error"]["data"]["code"] == "request_cancelled"
        assert response[1]["result"] == "pong"
        assert response[2]["error"]["data"]["code"] == "invalid_request"


@pytest.mark.parametrize(
    "invalid",
    [
        {"params": {"id": True}},
        {"params": {"id": None}},
        {"params": {"id": []}},
        {"params": {"id": 1, "extra": True}},
        {"params": [1]},
        {"jsonrpc": "1.0"},
        {"extra": True},
    ],
)
async def test_malformed_cancel_notifications_do_not_cancel_work(invalid):
    entered, release = asyncio.Event(), asyncio.Event()
    channel = RpcChannel("deadlines")

    @channel.method()
    async def work() -> str:
        entered.set()
        await release.wait()
        return "completed"

    app = RpcService(limits=RpcLimits(max_pending_requests=1))
    app.socket("/rpc", channels=[channel])
    async with deadline_socket(app) as (socket, _):
        await socket.client_send(json.dumps(deadline_request(1)))
        await entered.wait()
        await socket.client_send(json.dumps({**cancel_notification(1), **invalid}))
        await socket.client_send(json.dumps(deadline_request(2)))
        assert (
            json.loads(await socket.client_receive())["error"]["data"]["code"]
            == "pending_limit"
        )
        release.set()
        assert json.loads(await socket.client_receive())["result"] == "completed"


@pytest.mark.parametrize("batch", [False, True])
async def test_duplicate_in_flight_ids_close_without_overwriting_original(batch):
    entered, stopped = asyncio.Event(), asyncio.Event()
    channel = RpcChannel("deadlines")

    @channel.method()
    async def work() -> None:
        entered.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    app = RpcService()
    app.socket("/rpc", channels=[channel])
    async with deadline_socket(app) as (socket, serving):
        if batch:
            await socket.client_send(
                json.dumps([deadline_request(1), deadline_request(1)])
            )
        else:
            await socket.client_send(json.dumps(deadline_request(1)))
            await entered.wait()
            await socket.client_send(json.dumps(deadline_request(1)))
        await serving
        assert socket.closed == (
            RpcConnectionClose.PROTOCOL_ERROR,
            "Duplicate in-flight request id",
        )
        if not batch:
            assert stopped.is_set()


@pytest.mark.parametrize(
    "invalid",
    ["42", "{", "[]", '{"jsonrpc":"2.0","id":42,"method":"$/ping"}'],
)
async def test_cancel_is_processed_while_writer_and_outgoing_queue_are_full(invalid):
    from fastapi_canon.integrations.websockets.testing import SocketFaults

    entered, stopped = asyncio.Event(), asyncio.Event()
    recorder = DeadlineRecorder()
    channel = RpcChannel("deadlines")

    @channel.method()
    async def work() -> None:
        entered.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    faults = SocketFaults()
    faults.block()
    socket = InMemorySocket("/rpc", server_to_client=faults)
    app = RpcService(
        limits=RpcLimits(max_pending_requests=2, max_queue_size=1),
        middleware=[recorder],
    )
    app.socket("/rpc", channels=[channel])
    try:
        async with deadline_socket(app, socket) as (socket, _):
            await socket.client_send(json.dumps(deadline_request(1)))
            await entered.wait()
            await socket.client_send(invalid)
            await faults.blocked.wait()
            if "$/ping" not in invalid:
                await recorder.finished.get()
            await socket.client_send(invalid)
            if "$/ping" not in invalid:
                await recorder.finished.get()
            # The second invalid response fills the queue behind the blocked writer.
            await asyncio.sleep(0)
            if "$/ping" in invalid:
                await socket.client_send(invalid)
            await socket.client_send(json.dumps(cancel_notification(1)))
            await stopped.wait()
            faults.unblock()
    finally:
        faults.unblock()


@pytest.mark.parametrize("phase", ["enter", "exit"])
@pytest.mark.parametrize("ending", ["deadline", "cancel"])
async def test_middleware_interruption_returns_and_observes_failure(
    monkeypatch, phase, ending
):
    clock = ControlledDeadlines(monkeypatch)
    entered = asyncio.Event()
    recorder = DeadlineRecorder()

    class Blocking(RpcMiddleware):
        @asynccontextmanager
        async def request(self, scope):
            if phase == "enter":
                entered.set()
                await asyncio.Future()
            yield
            if phase == "exit":
                entered.set()
                await asyncio.Future()

    channel = RpcChannel("deadlines")

    @channel.method()
    async def work() -> int:
        assert phase != "enter"
        return 42

    app = RpcService(
        middleware=[recorder, Blocking()], limits=RpcLimits(request_timeout=5)
    )
    app.socket("/rpc", channels=[channel])
    async with deadline_socket(app) as (socket, _):
        await socket.client_send(json.dumps(deadline_request(1)))
        await entered.wait()
        _, waiting = await clock.next()
        if ending == "deadline":
            clock.expire(waiting)
        else:
            await socket.client_send(json.dumps(cancel_notification(1)))
        response = json.loads(await socket.client_receive())
        scope = await recorder.finished.get()
        expected = "request_timeout" if ending == "deadline" else "request_cancelled"
        assert response["error"]["data"]["code"] == expected
        assert scope.outcome.response.model_dump() == response
        assert scope.outcome.error.code == expected
        assert scope.outcome.cancelled is (ending == "cancel")
        assert scope.outcome.duration > 0


async def test_boolean_batch_id_cannot_alias_a_valid_request_id():
    channel = RpcChannel("deadlines")
    executions = []

    @channel.method()
    async def work() -> int:
        executions.append(True)
        return 42

    app = RpcService()
    app.socket("/rpc", channels=[channel])
    async with deadline_socket(app) as (socket, _):
        await socket.client_send(
            json.dumps([deadline_request(True), deadline_request(1)])
        )
        invalid, valid = json.loads(await socket.client_receive())
        assert invalid["id"] is None
        assert invalid["error"]["code"] == -32600
        assert valid == {"jsonrpc": "2.0", "id": 1, "result": 42}
        assert executions == [True]


async def test_response_frames_from_the_client_are_invalid_requests():
    channel = RpcChannel("deadlines")

    @channel.method()
    async def work() -> int:
        return 42

    app = RpcService()
    app.socket("/rpc", channels=[channel])
    async with deadline_socket(app) as (socket, _):
        await socket.client_send(
            json.dumps({"jsonrpc": "2.0", "id": "server:1", "result": True})
        )
        invalid = json.loads(await socket.client_receive())
        assert invalid["error"]["code"] == -32600
        await socket.client_send(json.dumps(deadline_request(1)))
        assert json.loads(await socket.client_receive())["result"] == 42


@pytest.mark.parametrize("ending", ["disconnect", "shutdown"])
async def test_shutdown_cancellation_releases_admission_without_cancelled_responses(
    monkeypatch, ending
):
    import fastapi_canon.integrations.websockets.runtime as runtime_module

    capacities = []
    original = runtime_module._PendingRequests

    def capture(limit):
        result = original(limit)
        capacities.append(result)
        return result

    monkeypatch.setattr(runtime_module, "_PendingRequests", capture)
    before = asyncio.all_tasks()
    entered, stopped = asyncio.Event(), asyncio.Event()
    recorder = DeadlineRecorder()
    channel = RpcChannel("deadlines")

    @channel.method()
    async def work() -> None:
        entered.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    app = RpcService(middleware=[recorder])
    app.socket("/rpc", channels=[channel])
    socket = InMemorySocket("/rpc")
    serving = asyncio.create_task(app.serve(socket))
    await socket.client_send(json.dumps(deadline_request(1)))
    await asyncio.wait_for(entered.wait(), 1)
    if ending == "shutdown":
        serving.cancel()
        with pytest.raises(asyncio.CancelledError):
            await serving
    else:
        await socket.client_disconnect()
        await serving
    assert stopped.is_set()
    scope = await recorder.finished.get()
    assert scope.outcome.cancelled
    assert isinstance(scope.outcome.error, asyncio.CancelledError)
    assert scope.outcome.response is None
    assert capacities[0].count == 0
    assert not asyncio.all_tasks() - before


class Params(RpcModel):
    value: str


channel = RpcChannel("demo")
connections = []


class Instrumentation(RpcMiddleware):
    def __init__(self) -> None:
        self.closed = []
        self.opened = []
        self.started = []
        self.slow = []

    @asynccontextmanager
    async def request(self, scope):
        self.started.append(scope.request)
        yield

    async def connection_opened(self, connection) -> None:
        self.opened.append(connection)

    async def slow_consumer_closed(self, connection) -> None:
        self.slow.append(connection)

    @asynccontextmanager
    async def connection(self, scope):
        try:
            yield
        finally:
            self.closed.append(scope)


instrumentation = Instrumentation()


@channel.method()
async def echo(params: Params, connection: Inject[RpcConnection]) -> Params:
    connections.append(connection)
    return params


service = RpcService(middleware=[instrumentation])
service.socket("/rpc", channels=(channel,))


async def test_request_and_connection_context() -> None:
    connections.clear()
    instrumentation.closed.clear()
    instrumentation.opened.clear()
    instrumentation.started.clear()
    async with RpcTestClient(service, "/rpc", headers={"Authorization": "x"}) as client:
        assert await client.request("demo.echo", {"value": "yes"}) == {"value": "yes"}
    assert connections[0].headers["authorization"] == "x"
    assert connections[0].closed
    assert connections[0].close_code == RpcConnectionClose.NORMAL
    assert connections[0].close_reason == ""
    assert instrumentation.closed[0].connection is connections[0]
    assert instrumentation.closed[0].outcome.close_code == RpcConnectionClose.NORMAL
    assert instrumentation.closed[0].outcome.duration >= 0
    assert instrumentation.opened == connections
    assert instrumentation.started[0].connection is connections[0]


async def test_client_close_information_is_exposed_on_the_connection() -> None:
    connections.clear()
    instrumentation.closed.clear()
    async with RpcTestClient(service, "/rpc") as client:
        await client.request("demo.echo", {"value": "yes"})
        await client.socket.client_disconnect(1001, "Going away")

    assert connections[0].close_code == RpcConnectionClose.SHUTDOWN
    assert connections[0].raw_close_code == 1001
    assert connections[0].close_reason == "Going away"
    assert instrumentation.closed[0].connection.close_reason == "Going away"


async def test_binary_stream() -> None:
    streams = RpcChannel("streams")

    class StreamMetrics(RpcMiddleware):
        def __init__(self) -> None:
            self.sent: list[int] = []

        async def stream_frame_sent(self, connection: RpcConnection, size: int) -> None:
            assert connection.path == "/frames"
            self.sent.append(size)

    @streams.stream()
    async def frames() -> AsyncIterator[bytes]:
        yield b"one"

    stream_metrics = StreamMetrics()
    rpc = RpcService(middleware=[stream_metrics])
    rpc.stream("/frames", frames)
    async with RpcTestClient(rpc, "/frames") as client:
        assert await client.next_frame() == b"one"
        while client.socket.closed is None:
            await asyncio.sleep(0)
        assert client.socket.closed == (RpcConnectionClose.NORMAL, "")
    assert stream_metrics.sent == [3]


async def test_unsupported_subprotocol_is_rejected_before_acceptance() -> None:
    rpc = RpcService()
    rpc.socket("/rpc", channels=(channel,), subprotocol="rpc.v2")

    async with RpcTestClient(rpc, "/rpc", subprotocols=("rpc.v1",)) as client:
        await client.closed()

    assert not client.socket.accepted
    assert client.socket.rejection == (
        RpcRejection.PROTOCOL_ERROR,
        "Unsupported subprotocol",
    )


async def test_socket_path_model_is_validated_and_injected() -> None:
    class RoomPath(RpcModel):
        room_id: UUID

    room = RpcChannel("room")

    @room.method()
    async def get(path: Inject[RoomPath]) -> str:
        return str(path.room_id)

    rpc = RpcService()
    rpc.socket("/rooms/{room_id}", channels=(room,), path_model=RoomPath)
    value = "bc1560c4-68c2-471f-b2f7-2ef3e6cc87bd"

    async with RpcTestClient(rpc, f"/rooms/{value}") as client:
        assert await client.request("room.get") == value

    async with RpcTestClient(rpc, "/rooms/invalid") as client:
        await client.closed()
        assert client.socket.rejection == (
            RpcRejection.NOT_FOUND,
            "Invalid path variable",
        )


def test_socket_path_model_fields_must_match_template() -> None:
    class WrongPath(RpcModel):
        other: UUID

    rpc = RpcService()
    with pytest.raises(ProtocolDefinitionError, match="must match path variables"):
        rpc.socket("/rooms/{room_id}", channels=(channel,), path_model=WrongPath)


def test_unknown_peer_close_code_is_preserved_separately() -> None:
    error = RpcDisconnect(4321, "custom")
    assert error.code == RpcConnectionClose.OTHER
    assert error.raw_close_code == 4321


@pytest.mark.parametrize("frame", ["{", "[" * 100_000], ids=["syntax", "deep"])
async def test_parse_error_is_answered_and_connection_remains_usable(frame, caplog):
    caplog.set_level(logging.DEBUG, logger="fastapi_canon.integrations.websockets")
    socket = InMemorySocket("/rpc")
    task = asyncio.create_task(service.serve(socket))
    await asyncio.sleep(0)

    await socket.client_send(frame)
    failure = json.loads(await socket.client_receive())
    await socket.client_send(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "demo.echo",
                "params": {"value": "still open"},
            }
        )
    )
    success = json.loads(await socket.client_receive())
    await socket.client_disconnect()
    await task

    assert failure["id"] is None
    assert failure["error"]["code"] == RpcErrorCode.PARSE_ERROR
    assert success == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"value": "still open"},
    }
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf"), "5"])
def test_request_timeout_limit_requires_positive_finite_seconds(timeout):
    with pytest.raises(ValueError, match="request_timeout"):
        RpcLimits(request_timeout=timeout)


async def test_pending_rejection_logs_are_bounded_even_at_debug(caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="fastapi_canon.integrations.websockets")
    entered, release = asyncio.Event(), asyncio.Event()
    channel = RpcChannel("flood")
    finished = []

    class Instrumentation(RpcMiddleware):
        @asynccontextmanager
        async def request(self, scope):
            try:
                yield
            finally:
                finished.append(scope.outcome)

    @channel.method
    async def wait() -> None:
        entered.set()
        await release.wait()

    app = RpcService(
        middleware=[Instrumentation()], limits=RpcLimits(max_pending_requests=1)
    )
    app.socket("/rpc", channels=[channel])
    socket = InMemorySocket("/rpc")
    serving = asyncio.create_task(app.serve(socket))
    try:
        await socket.client_send(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "flood.wait"})
        )
        await entered.wait()
        for request_id in range(2, 62):
            await socket.client_send(
                json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "flood.wait"})
            )
        for _ in range(60):
            response = json.loads(await socket.client_receive())
            assert response["error"]["data"]["code"] == "pending_limit"
        release.set()
        assert json.loads(await socket.client_receive())["id"] == 1
    finally:
        release.set()
        await socket.client_disconnect()
        await serving
    records = [r for r in caplog.records if "max_pending_requests" in r.message]
    assert len(records) == 2
    assert [r.levelno for r in records] == [logging.WARNING, logging.DEBUG]
    assert "60" in records[-1].message
    assert (
        len([c for c in finished if c.response and hasattr(c.response, "error")]) == 60
    )


async def test_requests_beyond_the_pending_limit_are_rejected() -> None:
    pending = RpcChannel("pending")
    release = asyncio.Event()

    @pending.method()
    async def wait(params: Params) -> Params:
        await release.wait()
        return params

    pending_service = RpcService()
    pending_service.socket("/rpc", channels=(pending,))
    socket = InMemorySocket("/rpc")
    task = asyncio.create_task(
        pending_service.serve(
            socket, limits=RpcLimits(max_concurrency=1, max_pending_requests=2)
        )
    )
    await asyncio.sleep(0)

    for request_id in (1, 2, 3):
        await socket.client_send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "pending.wait",
                    "params": {"value": str(request_id)},
                }
            )
        )
    rejected = json.loads(await asyncio.wait_for(socket.client_receive(), 1))
    release.set()
    answered = [
        json.loads(await asyncio.wait_for(socket.client_receive(), 1)) for _ in range(2)
    ]
    await socket.client_disconnect()
    await task

    assert rejected["id"] == 3
    assert rejected["error"]["message"] == "Too many pending requests"
    assert sorted(item["id"] for item in answered) == [1, 2]


async def test_batches_reserve_capacity_per_item_and_preserve_response_shape():
    release = asyncio.Event()
    started = asyncio.Queue()
    channel = RpcChannel("capacity")

    @channel.method
    async def wait(params: Params) -> Params:
        started.put_nowait(params.value)
        await release.wait()
        return params

    app = RpcService(limits=RpcLimits(max_concurrency=2, max_pending_requests=2))
    app.socket("/rpc", channels=[channel])
    socket = InMemorySocket("/rpc")
    serving = asyncio.create_task(app.serve(socket))

    def request(value, request_id=None):
        message = {
            "jsonrpc": "2.0",
            "method": "capacity.wait",
            "params": {"value": value},
        }
        if request_id is not None:
            message["id"] = request_id
        return message

    try:
        await socket.client_send(
            json.dumps(
                [request("first", 1), request("notification"), request("excess", 2)]
            )
        )
        async with asyncio.timeout(1):
            assert {await started.get(), await started.get()} == {
                "first",
                "notification",
            }
            await socket.client_send(json.dumps(request("single", 3)))
            rejected = json.loads(await socket.client_receive())
            assert rejected["id"] == 3
            assert rejected["error"]["data"]["code"] == "pending_limit"
            await socket.client_send(
                json.dumps([request("batch", 4), request("dropped"), 42])
            )
            batch = json.loads(await socket.client_receive())
            assert isinstance(batch, list)
            assert [r["id"] for r in batch] == [4, None]
            assert batch[0]["error"]["data"]["code"] == "pending_limit"
            assert batch[1]["error"]["data"]["code"] == "invalid_request"
        release.set()
        batch = json.loads(await socket.client_receive())
        assert [r["id"] for r in batch] == [1, 2]
        assert batch[0]["result"] == {"value": "first"}
        assert batch[1]["error"]["data"]["code"] == "pending_limit"
        await socket.client_send(json.dumps(request("after", 5)))
        assert json.loads(await socket.client_receive())["result"] == {"value": "after"}
        assert started.qsize() == 1  # Only the new call ran; rejected work never ran.
    finally:
        release.set()
        await socket.client_disconnect()
        await serving


@pytest.mark.parametrize("frame", [[], [42], [[{"id": 1}]], [None] * 33])
async def test_invalid_batches_do_not_consume_admission_or_create_nested_batches(frame):
    socket = InMemorySocket("/rpc")
    serving = asyncio.create_task(service.serve(socket))
    try:
        await socket.client_send(json.dumps(frame))
        response = json.loads(await socket.client_receive())
        failures = response if isinstance(response, list) else [response]
        assert all(f["error"]["data"]["code"] == "invalid_request" for f in failures)
        await socket.client_send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "demo.echo",
                    "params": {"value": "ok"},
                }
            )
        )
        assert json.loads(await socket.client_receive())["result"] == {"value": "ok"}
    finally:
        await socket.client_disconnect()
        await serving


@pytest.mark.parametrize("ending", ["success", "failure", "cancel", "disconnect"])
async def test_item_capacity_is_released_after_every_outcome(ending, monkeypatch):
    import fastapi_canon.integrations.websockets.runtime as runtime_module
    from fastapi_canon.integrations.websockets import RpcInvalidParamsError

    capacities = []
    original = runtime_module._PendingRequests

    def capacity(limit):
        result = original(limit)
        capacities.append(result)
        return result

    monkeypatch.setattr(runtime_module, "_PendingRequests", capacity)
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    channel = RpcChannel("release")

    @channel.method
    async def work() -> None:
        entered.set()
        try:
            await release.wait()
            if ending == "failure":
                raise RpcInvalidParamsError(message="controlled failure")
            if ending == "cancel":
                raise asyncio.CancelledError
        finally:
            finished.set()

    @channel.method
    async def ready() -> str:
        return "ok"

    app = RpcService(limits=RpcLimits(max_pending_requests=1))
    app.socket("/rpc", channels=[channel])
    socket = InMemorySocket("/rpc")
    serving = asyncio.create_task(app.serve(socket))
    try:
        await socket.client_send(
            json.dumps(
                [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "release.work",
                    }
                ]
            )
        )
        await entered.wait()
        assert capacities[0].count == 1
        if ending == "disconnect":
            await socket.client_disconnect()
            await serving
        else:
            release.set()
            await finished.wait()
            if ending != "cancel":
                await socket.client_receive()
            async with asyncio.timeout(1):
                while capacities[0].count:
                    await asyncio.sleep(0)
            await socket.client_send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "release.ready",
                    }
                )
            )
            assert json.loads(await socket.client_receive())["result"] == "ok"
    finally:
        release.set()
        await socket.client_disconnect()
        await serving
    assert finished.is_set()
    assert capacities[0].count == 0


def test_reservation_release_is_idempotent_and_never_negative():
    from fastapi_canon.integrations.websockets.runtime import _PendingRequests

    capacity = _PendingRequests(1)
    reservation = capacity.reserve()
    assert reservation is not None
    assert capacity.reserve() is None
    capacity.release(reservation)
    capacity.release(reservation)
    assert capacity.count == 0
    assert capacity.reserve() is not None


@pytest.mark.parametrize("ending", ["failure", "disconnect", "shutdown"])
async def test_documented_shared_capacity_releases_worker_permits(ending):
    from fastapi_canon.integrations.websockets import RpcInvalidParamsError

    guide = (Path(__file__).parents[1] / "docs/services.md").read_text(encoding="utf-8")
    snippet = guide.split("## Capacity across connections", 1)[1].split("```python", 1)[
        1
    ]
    namespace = {}
    exec(snippet.split("```", 1)[0], namespace)
    queued = asyncio.Event()

    class Capacity(namespace["SharedCapacity"]):
        @asynccontextmanager
        async def request(self, scope):
            if scope.request.request_id == 2:
                queued.set()
            async with super().request(scope):
                yield

    capacity = Capacity(connections=2, requests=1)
    started = asyncio.Queue()
    release = asyncio.Event()
    channel = RpcChannel("shared")

    @channel.method
    async def wait(params: Params) -> None:
        started.put_nowait(params.value)
        await release.wait()
        raise RpcInvalidParamsError(message="controlled failure")

    app = RpcService(middleware=[capacity])
    app.socket("/rpc", channels=[channel], before_accept=capacity.before_accept)
    sockets = [InMemorySocket("/rpc") for _ in range(3)]
    serving = []
    try:
        for i, socket in enumerate(sockets[:2], 1):
            serving.append(asyncio.create_task(app.serve(socket)))
            await socket.client_send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": i,
                        "method": "shared.wait",
                        "params": {"value": str(i)},
                    }
                )
            )
            if i == 1:
                assert await started.get() == "1"
            else:
                await queued.wait()
        assert capacity.active_connections == 2
        assert capacity.active_requests == 1
        assert started.empty()
        await app.serve(sockets[2])
        assert sockets[2].rejection[0] == RpcRejection.UNAVAILABLE
        assert capacity.active_connections == 2
        if ending == "failure":
            release.set()
            await sockets[0].client_receive()
        elif ending == "disconnect":
            await sockets[0].client_disconnect()
            await serving[0]
        else:
            serving[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await serving[0]
        assert await started.get() == "2"
    finally:
        release.set()
        for socket in sockets:
            await socket.client_disconnect()
        await asyncio.gather(*serving, return_exceptions=True)
    assert capacity.active_connections == 0
    assert capacity.active_requests == 0


async def test_writer_failure_closes_connection_and_finishes_middleware() -> None:
    class FailingSocket(InMemorySocket):
        async def send(self, message: str) -> None:
            raise RuntimeError("send failed")

    instrumentation.closed.clear()
    socket = FailingSocket("/rpc")
    task = asyncio.create_task(service.serve(socket))
    await socket.client_send(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "demo.echo",
                "params": {"value": "x"},
            }
        )
    )
    await asyncio.wait_for(task, 1)

    assert socket.closed == (RpcConnectionClose.INTERNAL_ERROR, "Internal error")
    assert (
        instrumentation.closed[-1].outcome.close_code
        == RpcConnectionClose.INTERNAL_ERROR
    )


async def test_slow_socket_send_closes_with_policy_violation() -> None:
    instrumentation.slow.clear()

    class SlowSocket(InMemorySocket):
        async def send(self, message: str) -> None:
            await asyncio.Event().wait()

    socket = SlowSocket("/rpc")
    task = asyncio.create_task(
        service.serve(socket, limits=RpcLimits(send_timeout=0.01))
    )
    await socket.client_send(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "demo.echo",
                "params": {"value": "x"},
            }
        )
    )
    await asyncio.wait_for(task, 1)

    assert socket.closed == (RpcConnectionClose.POLICY_VIOLATION, "Client too slow")
    assert len(instrumentation.slow) == 1
    assert instrumentation.slow[0] is instrumentation.closed[-1].connection


@pytest.mark.parametrize(
    ("frame", "expected"),
    [
        (b"\xff", (RpcConnectionClose.PROTOCOL_ERROR, "Invalid RPC frame")),
        ("12345", (RpcConnectionClose.MESSAGE_TOO_BIG, "")),
        ("ééé", (RpcConnectionClose.MESSAGE_TOO_BIG, "")),
        (b"12345", (RpcConnectionClose.MESSAGE_TOO_BIG, "")),
    ],
)
async def test_invalid_or_oversized_rpc_frames_close_the_connection(
    frame: str | bytes, expected: tuple[RpcConnectionClose, str]
) -> None:
    async with RpcTestClient(
        service, "/rpc", limits=RpcLimits(max_message_bytes=4)
    ) as client:
        await client.socket.client_send(frame)
        await client.closed()

    assert client.socket.closed == expected


async def test_server_events_are_sent_as_typed_notifications() -> None:
    events = RpcChannel("events")
    ready = asyncio.Event()

    @events.event(payload=Params)
    async def changed(trigger: Inject[asyncio.Event]) -> AsyncIterator[Params]:
        await trigger.wait()
        yield Params(value="ready")
        await asyncio.Event().wait()

    class EventMetrics(RpcMiddleware):
        def __init__(self) -> None:
            self.sent: list[tuple[str, int]] = []

        async def notification_sent(self, name: str, size: int) -> None:
            self.sent.append((name, size))

    event_metrics = EventMetrics()
    rpc = RpcService(middleware=[event_metrics])
    rpc.socket("/events", channels=(events,))

    async with RpcTestClient(rpc, "/events", context=ready) as client:
        ready.set()
        notification = await client.next_notification()

    assert notification == ("events.changed", {"value": "ready"})
    assert event_metrics.sent[0][0] == "events.changed"
    assert event_metrics.sent[0][1] > 0


async def test_invalid_server_event_does_not_close_connection() -> None:
    events = RpcChannel("events")
    ready = asyncio.Event()

    @events.event(payload=Params)
    async def changed() -> AsyncIterator[Params]:
        yield {}  # type: ignore[misc]

    @events.event(payload=Params)
    async def healthy() -> AsyncIterator[Params]:
        await ready.wait()
        yield Params(value="still running")

    rpc = RpcService()
    rpc.socket("/events", channels=(events,))

    async with RpcTestClient(rpc, "/events") as client:
        ready.set()
        notification = await asyncio.wait_for(client.next_notification(), 1)
        assert notification == ("events.healthy", {"value": "still running"})


async def test_event_can_opt_in_to_closing_on_error() -> None:
    events = RpcChannel("events")

    @events.event(payload=Params, on_error="close")
    async def changed() -> AsyncIterator[Params]:
        yield {}  # type: ignore[misc]

    rpc = RpcService()
    rpc.socket("/events", channels=(events,))

    async with RpcTestClient(rpc, "/events") as client:
        await asyncio.wait_for(client.closed(), 1)

    assert client.socket.closed == (
        RpcConnectionClose.INTERNAL_ERROR,
        "Internal error",
    )


async def test_mapped_event_source_failure_closes_the_connection() -> None:
    class AccessRevoked(Exception):
        pass

    events = RpcChannel("events")

    @events.event(payload=Params)
    async def changed() -> AsyncIterator[Params]:
        raise AccessRevoked("revoked")
        yield Params(value="never")

    rpc = RpcService()
    rpc.socket("/events", channels=(events,))

    async with RpcTestClient(
        rpc, "/events", rejections={AccessRevoked: RpcRejection.FORBIDDEN}
    ) as client:
        await asyncio.wait_for(client.closed(), 1)

    assert client.socket.closed == (RpcConnectionClose.POLICY_VIOLATION, "revoked")


async def test_before_accept_failures_are_rejected_with_mapped_rejections() -> None:
    class SessionNotFound(Exception):
        pass

    async def authenticate(handshake: RpcHandshake) -> None:
        raise SessionNotFound("no session")

    rpc = RpcService()
    rpc.socket("/rpc", channels=(channel,), before_accept=authenticate)

    async with RpcTestClient(
        rpc, "/rpc", rejections={SessionNotFound: RpcRejection.NOT_FOUND}
    ) as client:
        await client.closed()

    assert not client.socket.accepted
    assert client.socket.rejection == (RpcRejection.NOT_FOUND, "no session")


async def test_cancelling_a_connection_closes_it_as_shutdown() -> None:
    socket = InMemorySocket("/rpc")
    task = asyncio.create_task(service.serve(socket))
    await asyncio.sleep(0)
    assert socket.accepted

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert socket.closed == (RpcConnectionClose.SHUTDOWN, "")


class DelayedSocket(InMemorySocket):
    async def send(self, message: str) -> None:
        await asyncio.sleep(0.01)
        await super().send(message)


async def test_closing_after_the_final_event_delivers_queued_notifications() -> None:
    events = RpcChannel("events")

    @events.event(payload=Params)
    async def finished(
        connection: Inject[RpcConnection],
    ) -> AsyncIterator[Params]:
        yield Params(value="last")
        await connection.close()

    rpc = RpcService()
    rpc.socket("/events", channels=(events,))
    socket = DelayedSocket("/events")

    await asyncio.wait_for(rpc.serve(socket), 1)

    assert json.loads(await socket.client_receive())["params"] == {"value": "last"}
    assert socket.closed == (RpcConnectionClose.NORMAL, "")


async def test_socket_closes_when_its_events_complete() -> None:
    events = RpcChannel("events")

    @events.event(payload=Params)
    async def first() -> AsyncIterator[Params]:
        yield Params(value="first")

    @events.event(payload=Params)
    async def second() -> AsyncIterator[Params]:
        await asyncio.sleep(0.01)
        yield Params(value="second")

    rpc = RpcService()
    rpc.socket("/events", channels=(events,), close_when_events_complete=True)
    socket = DelayedSocket("/events")

    await asyncio.wait_for(rpc.serve(socket), 1)

    received = [json.loads(await socket.client_receive()) for _ in range(2)]
    assert [message["params"]["value"] for message in received] == [
        "first",
        "second",
    ]
    assert socket.closed == (RpcConnectionClose.NORMAL, "")


class SessionExpired(Exception):
    pass


class SessionNotFound(Exception):
    pass


def rejects(exception: type[Exception], rejection: RpcRejection) -> RpcErrorBinding:
    return RpcErrorBinding(exception, message=str, rejection=rejection)


@pytest.mark.parametrize(
    ("failure", "call_rejections", "expected"),
    [
        (SessionExpired("expired"), None, RpcRejection.UNAUTHORIZED),
        (SessionNotFound("missing"), None, RpcRejection.NOT_FOUND),
        (
            SessionExpired("expired"),
            {SessionExpired: RpcRejection.FORBIDDEN},
            RpcRejection.FORBIDDEN,
        ),
        (
            SessionNotFound("missing"),
            {SessionExpired: RpcRejection.FORBIDDEN},
            RpcRejection.NOT_FOUND,
        ),
        (SessionNotFound("missing"), lambda error: None, RpcRejection.NOT_FOUND),
    ],
)
async def test_rejections_are_looked_up_from_call_to_endpoint_to_service(
    failure: Exception, call_rejections, expected: RpcRejection
) -> None:
    async def authenticate(handshake: RpcHandshake) -> None:
        raise failure

    rpc = RpcService(
        rejects=[
            rejects(SessionExpired, RpcRejection.UNAUTHORIZED),
            rejects(SessionNotFound, RpcRejection.UNAVAILABLE),
        ]
    )
    rpc.socket(
        "/rpc",
        channels=(RpcChannel("sessions"),),
        before_accept=authenticate,
        rejects=[rejects(SessionNotFound, RpcRejection.NOT_FOUND)],
    )

    async with RpcTestClient(rpc, "/rpc", rejections=call_rejections) as client:
        await client.closed()

    assert client.socket.rejection == (expected, str(failure))


async def test_stream_endpoints_apply_their_contracts() -> None:
    audio = RpcChannel("audio")

    @audio.stream()
    async def frames() -> AsyncIterator[bytes]:
        raise SessionNotFound("missing")
        yield b""

    rpc = RpcService()
    rpc.stream(
        "/audio", frames, rejects=[rejects(SessionNotFound, RpcRejection.UNAVAILABLE)]
    )

    async with RpcTestClient(rpc, "/audio") as client:
        await asyncio.wait_for(client.closed(), 1)

    assert client.socket.closed == (RpcConnectionClose.TRY_AGAIN_LATER, "missing")


async def test_contracts_reject_with_their_message_by_default() -> None:
    async def authenticate(handshake: RpcHandshake) -> None:
        raise SessionNotFound("internal lookup detail")

    rpc = RpcService()
    rpc.socket(
        "/rpc",
        channels=(RpcChannel("sessions"),),
        before_accept=authenticate,
        rejects=[RpcErrorBinding(SessionNotFound, rejection=RpcRejection.NOT_FOUND)],
    )

    async with RpcTestClient(rpc, "/rpc") as client:
        await client.closed()

    assert client.socket.rejection == (RpcRejection.NOT_FOUND, "Session not found")


@pytest.mark.parametrize("declared", [True, False])
async def test_event_bindings_are_scoped_and_exported(declared, caplog) -> None:
    binding = RpcErrorBinding(
        SessionNotFound,
        message="Session unavailable",
        rejection=RpcRejection.UNAVAILABLE,
    )
    fallback = RpcErrorBinding(
        SessionNotFound, code="session_denied", rejection=RpcRejection.FORBIDDEN
    )
    channel = RpcChannel("session", raises=[fallback] if declared else [])

    @channel.event(raises=[binding] if declared else [], on_error="close")
    async def updates() -> AsyncIterator[Params]:
        raise SessionNotFound("private lookup text")
        yield Params(value="never")

    @channel.method(raises=[binding])
    async def lookup() -> None: ...

    service = RpcService()
    service.socket("/rpc", channels=[channel])
    document = service.contract(
        title="Sessions", base_url="ws://localhost"
    ).to_openrpc()
    assert ("errors" in document["x-rpc-notifications"][0]) is declared
    if declared:
        assert [
            error["x-rpckit-code"]
            for error in document["x-rpc-notifications"][0]["errors"]
        ] == [binding.code]
    async with RpcTestClient(service, "/rpc") as client:
        await asyncio.wait_for(client.closed(), 1)
    assert client.socket.closed == (
        (RpcConnectionClose.TRY_AGAIN_LATER, "Session unavailable")
        if declared
        else (RpcConnectionClose.INTERNAL_ERROR, "Internal error")
    )
    if not declared:
        assert "RPC method session.updates failed" in caplog.text


@pytest.mark.parametrize(
    "raises",
    [
        [RpcErrorBinding(SessionNotFound)],
        [SessionNotFound],
        [
            rejects(SessionNotFound, RpcRejection.NOT_FOUND),
            rejects(SessionNotFound, RpcRejection.UNAVAILABLE),
        ],
    ],
)
def test_endpoint_rejects_takes_one_rejecting_binding_per_exception(raises) -> None:
    with pytest.raises(ProtocolDefinitionError):
        RpcService().socket("/rpc", channels=(RpcChannel("sessions"),), rejects=raises)


async def test_request_envelope_is_validated_once(monkeypatch) -> None:
    from fastapi_canon.integrations.websockets.envelopes import RpcRequestEnvelope

    calls = []
    original = RpcRequestEnvelope.model_validate

    def counted(value, *args, **kwargs):
        calls.append(value)
        return original(value, *args, **kwargs)

    monkeypatch.setattr(RpcRequestEnvelope, "model_validate", counted)
    async with RpcTestClient(service, "/rpc") as client:
        assert await client.request("demo.echo", {"value": "x"}) == {"value": "x"}

    assert len(calls) == 1
