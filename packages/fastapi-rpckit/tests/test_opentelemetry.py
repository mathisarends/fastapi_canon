import asyncio
import subprocess
import sys
from collections.abc import AsyncIterator

import pytest
from fastapi import APIRouter, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.testclient import TestClient
from fastapi_canon.integrations.websockets import (
    Inject,
    RpcBinaryInput,
    RpcBinaryOutput,
    RpcChannel,
    RpcConnectionClose,
    RpcLimits,
    RpcRequestScope,
    RpcService,
)
from fastapi_canon.integrations.websockets.adapters.fastapi import (
    RpcRoutes,
    serve_websocket,
)
from fastapi_canon.integrations.websockets.adapters.opentelemetry import OpenTelemetry
from fastapi_canon.integrations.websockets.testing import (
    InMemorySocket,
    RpcTestClient,
    RpcTestError,
)
from opentelemetry import baggage, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF
from opentelemetry.trace import SpanKind, StatusCode


@pytest.fixture
def providers():
    exporter = InMemorySpanExporter()
    tracer = TracerProvider()
    tracer.add_span_processor(SimpleSpanProcessor(exporter))
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader])
    yield tracer, meter, exporter, reader
    tracer.shutdown()
    meter.shutdown()


def points(reader, name):
    data = reader.get_metrics_data()
    if data is None:
        return []
    return [
        point
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == name
        for point in metric.data.data_points
    ]


def total(reader, name, **labels):
    return sum(
        point.value
        for point in points(reader, name)
        if all(point.attributes.get(k) == v for k, v in labels.items())
    )


@pytest.mark.parametrize("include_baggage", [False, True])
async def test_per_request_contexts_are_remote_independent_and_detached(
    providers, include_baggage
):
    tracer, meter, exporter, _ = providers
    channel = RpcChannel("trace")
    entered = asyncio.Queue()
    release = asyncio.Event()
    baggage_values = []

    @channel.method()
    async def wait() -> str:
        baggage_values.append(baggage.get_baggage("tenant"))
        await entered.put(trace.get_current_span().get_span_context().trace_id)
        await release.wait()
        return "ok"

    server = channel.create_server(
        middleware=[
            OpenTelemetry(
                tracer_provider=tracer,
                meter_provider=meter,
                propagate_baggage=include_baggage,
            )
        ]
    )
    parent_ids = ["1" * 32, "2" * 32]
    requests = [
        {
            "jsonrpc": "2.0",
            "id": i,
            "method": "trace.wait",
            "meta": {
                "traceparent": f"00-{trace_id}-{'3' * 16}-01",
                "baggage": "tenant=private",
            },
        }
        for i, trace_id in enumerate(parent_ids, 1)
    ]
    batch = asyncio.create_task(server.handle(requests))
    assert {await entered.get(), await entered.get()} == {
        int(x, 16) for x in parent_ids
    }
    assert not trace.get_current_span().get_span_context().is_valid
    release.set()
    assert all(result.result == "ok" for result in await batch)
    assert baggage_values == (["private"] * 2 if include_baggage else [None] * 2)
    assert baggage.get_baggage("tenant") is None
    release.clear()
    task = asyncio.create_task(server.handle(requests[0]))
    await entered.get()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not trace.get_current_span().get_span_context().is_valid
    release.set()
    await server.handle(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "trace.wait",
            "meta": {"traceparent": "invalid"},
        }
    )
    last = exporter.get_finished_spans()[-1]
    assert last.parent is None
    assert last.context.trace_id not in {int(x, 16) for x in parent_ids}
    assert all(
        "private" not in str(span.attributes) for span in exporter.get_finished_spans()
    )


async def test_request_metadata_overrides_handshake_and_links_connection(providers):
    import json

    tracer, meter, exporter, _ = providers
    channel = RpcChannel("trace")

    @channel.method()
    async def ping() -> str:
        return "pong"

    service = RpcService(
        middleware=[
            OpenTelemetry(
                tracer_provider=tracer, meter_provider=meter, connection_spans=True
            )
        ]
    )
    service.socket("/rpc", channels=[channel])
    handshake_id, request_id = "1" * 32, "2" * 32
    socket = InMemorySocket(
        "/rpc", headers={"traceparent": f"00-{handshake_id}-{'3' * 16}-01"}
    )
    serving = asyncio.create_task(service.serve(socket))
    try:
        await socket._handshake_complete.wait()
        for identifier, metadata in [
            (1, {"traceparent": f"00-{request_id}-{'4' * 16}-01"}),
            (2, None),
            (3, {}),
        ]:
            request = {"jsonrpc": "2.0", "id": identifier, "method": "trace.ping"}
            if metadata is not None:
                request["meta"] = metadata
            await socket.client_send(json.dumps(request))
            assert json.loads(await socket.client_receive())["result"] == "pong"
    finally:
        await socket.client_disconnect()
        await serving
    spans = exporter.get_finished_spans()
    connection = next(span for span in spans if span.name == "WS /rpc")
    calls = [span for span in spans if span.name == "trace.ping"]
    assert calls[0].context.trace_id == int(request_id, 16)
    assert calls[0].parent.is_remote
    assert calls[0].parent.span_id == int("4" * 16, 16)
    assert calls[0].links[0].context.span_id == connection.context.span_id
    assert calls[1].context.trace_id == int(handshake_id, 16)
    assert calls[1].parent.span_id == connection.context.span_id
    assert not calls[1].links
    assert calls[2].parent is None
    assert calls[2].context.trace_id not in {int(handshake_id, 16), int(request_id, 16)}
    assert calls[2].links[0].context.span_id == connection.context.span_id
    assert not trace.get_current_span().get_span_context().is_valid


@pytest.mark.parametrize("include_baggage", [False, True])
async def test_remote_handshake_baggage_requires_opt_in(providers, include_baggage):
    tracer, meter, _, _ = providers
    channel = RpcChannel("trace")

    @channel.method()
    async def baggage_value() -> str | None:
        return baggage.get_baggage("tenant")

    service = RpcService(
        middleware=[
            OpenTelemetry(
                tracer_provider=tracer,
                meter_provider=meter,
                propagate_baggage=include_baggage,
            )
        ]
    )
    service.socket("/rpc", channels=[channel])
    async with RpcTestClient(
        service, "/rpc", headers={"baggage": "tenant=private"}
    ) as client:
        result = await client.request("trace.baggage_value")
        assert result == ("private" if include_baggage else None)
    assert baggage.get_baggage("tenant") is None


async def test_subscription_metadata_parents_controls_and_generator(providers):
    import json

    tracer, meter, exporter, _ = providers
    channel = RpcChannel("updates")
    stream_parent = asyncio.get_running_loop().create_future()

    @channel.subscription()
    async def values() -> AsyncIterator[int]:
        stream_parent.set_result(trace.get_current_span().get_span_context())
        yield 1
        await asyncio.Event().wait()

    service = RpcService(
        middleware=[OpenTelemetry(tracer_provider=tracer, meter_provider=meter)]
    )
    service.socket("/rpc", channels=[channel])
    socket = InMemorySocket("/rpc")
    serving = asyncio.create_task(service.serve(socket))
    try:
        await socket.client_send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "updates.values.subscribe",
                    "meta": {"traceparent": f"00-{'1' * 32}-{'2' * 16}-01"},
                }
            )
        )
        ack = json.loads(await socket.client_receive())
        await socket.client_receive()  # First payload is outside the control span.
        parent = await stream_parent
        await socket.client_send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "updates.values.unsubscribe",
                    "params": ack["result"],
                    "meta": {"traceparent": f"00-{'3' * 32}-{'4' * 16}-01"},
                }
            )
        )
        assert json.loads(await socket.client_receive())["result"] is None
    finally:
        await socket.client_disconnect()
        await serving
    spans = {span.name: span for span in exporter.get_finished_spans()}
    subscribed = spans["updates.values.subscribe"]
    assert subscribed.context.trace_id == int("1" * 32, 16)
    assert parent.span_id == subscribed.context.span_id
    assert spans["updates.values.unsubscribe"].context.trace_id == int("3" * 32, 16)
    assert not trace.get_current_span().get_span_context().is_valid


async def test_stream_tracing_and_metrics_are_live_and_parent_child(providers):
    tracer, meter, exporter, reader = providers
    instrumentation = OpenTelemetry(tracer_provider=tracer, meter_provider=meter)
    channel = RpcChannel("media")
    custom = tracer.get_tracer("application")

    @channel.stream()
    async def frames(session_id: str) -> AsyncIterator[bytes]:
        with custom.start_as_current_span("encode"):
            yield b"one"
        await asyncio.Event().wait()

    rpc = RpcService(middleware=[instrumentation])
    rpc.stream("/sessions/{session_id}/media", frames)
    async with RpcTestClient(rpc, "/sessions/secret/media") as client:
        assert await client.next_frame() == b"one"
        assert total(reader, "rpckit.stream.frames") == 1
        assert total(reader, "rpckit.stream.bytes") == 3
        assert total(reader, "rpckit.streams.active") == 1
        assert total(reader, "rpckit.connections.active") == 1
        assert all(
            span.name != "media.frames" for span in exporter.get_finished_spans()
        )
    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert spans["encode"].parent.span_id == spans["media.frames"].context.span_id
    assert spans["media.frames"].kind is SpanKind.INTERNAL
    assert spans["media.frames"].status.status_code is StatusCode.UNSET
    assert total(reader, "rpckit.streams.active") == 0
    assert total(reader, "rpckit.connections.active") == 0
    assert total(reader, "rpckit.streams") == 1
    assert "secret" not in str(spans["media.frames"].attributes)
    assert "jsonrpc" not in str(spans["media.frames"].attributes)


async def test_duplex_counts_empty_messages_and_excludes_input_end(providers):
    tracer, meter, exporter, reader = providers
    channel = RpcChannel("voice")

    @channel.stream()
    async def echo(
        frames: Inject[RpcBinaryInput], output: Inject[RpcBinaryOutput]
    ) -> None:
        async for frame in frames:
            await output.send(frame)

    rpc = RpcService(
        middleware=[OpenTelemetry(tracer_provider=tracer, meter_provider=meter)]
    )
    rpc.stream("/voice", echo)
    async with RpcTestClient(rpc, "/voice") as client:
        for frame in (b"", b"abc"):
            await client.send_frame(frame)
            assert await client.next_frame() == frame
        await client.end_input()
        await client.closed()
    for direction in ("inbound", "outbound"):
        assert (
            total(
                reader,
                "rpckit.stream.frames",
                **{"rpckit.direction": direction},
            )
            == 2
        )
        assert (
            total(
                reader,
                "rpckit.stream.bytes",
                **{"rpckit.direction": direction},
            )
            == 3
        )
    assert len(exporter.get_finished_spans()) == 1


@pytest.mark.parametrize("record_exceptions", [False, True])
async def test_stream_failures_preserve_original_exception(
    providers, record_exceptions
):
    tracer, meter, exporter, reader = providers
    channel = RpcChannel("media")

    @channel.stream()
    async def broken() -> AsyncIterator[bytes]:
        yield b"first"
        raise ValueError("private error text")

    rpc = RpcService(
        middleware=[
            OpenTelemetry(
                tracer_provider=tracer,
                meter_provider=meter,
                record_exceptions=record_exceptions,
            )
        ]
    )
    rpc.stream("/broken", broken)
    async with RpcTestClient(rpc, "/broken") as client:
        assert await client.next_frame() == b"first"
        await client.closed()
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["error.type"] == "internal_error"
    assert bool(span.events) is record_exceptions
    assert total(reader, "rpckit.streams.active") == 0


async def test_requests_have_separate_spans_with_transport_parent(providers):
    tracer, meter, exporter, reader = providers
    channel = RpcChannel("demo")
    custom = tracer.get_tracer("application")

    @channel.method()
    async def ping() -> str:
        with custom.start_as_current_span("child"):
            await asyncio.sleep(0)
        return "pong"

    server = channel.create_server(
        middleware=[OpenTelemetry(tracer_provider=tracer, meter_provider=meter)]
    )
    with custom.start_as_current_span("transport") as parent:
        result = await server.handle(
            [{"jsonrpc": "2.0", "id": i, "method": "demo.ping"} for i in (1, 2)]
        )
        assert all(response.result == "pong" for response in result)
        assert trace.get_current_span() is parent
    spans = exporter.get_finished_spans()
    calls = [span for span in spans if span.name == "demo.ping"]
    children = [span for span in spans if span.name == "child"]
    assert len(calls) == len(children) == 2
    assert all(
        call.parent.span_id == parent.get_span_context().span_id for call in calls
    )
    assert {child.parent.span_id for child in children} == {
        call.context.span_id for call in calls
    }
    assert all(call.kind is SpanKind.SERVER for call in calls)
    assert total(reader, "rpckit.rpc.calls") == 2
    assert total(reader, "rpckit.rpc.active") == 0


async def test_handshake_context_is_extracted_and_unknown_names_are_not_recorded(
    providers,
):
    tracer, meter, exporter, reader = providers
    channel = RpcChannel("demo")

    @channel.method()
    async def ping() -> str:
        return "pong"

    rpc = RpcService(
        middleware=[OpenTelemetry(tracer_provider=tracer, meter_provider=meter)]
    )
    rpc.socket("/rpc", channels=[channel])
    trace_id = "12345678901234567890123456789012"
    parent_id = "1234567890123456"
    async with RpcTestClient(
        rpc, "/rpc", headers={"TraceParent": f"00-{trace_id}-{parent_id}-01"}
    ) as client:
        assert await client.request("demo.ping") == "pong"
        with pytest.raises(RpcTestError):
            await client.request("attacker-controlled-private-name")
    calls = exporter.get_finished_spans()
    assert len(calls) == 2
    assert all(span.context.trace_id == int(trace_id, 16) for span in calls)
    assert all(span.parent.span_id == int(parent_id, 16) for span in calls)
    unknown = next(span for span in calls if span.name == "RPC")
    assert "rpc.method" not in unknown.attributes
    assert "attacker-controlled" not in str(unknown.attributes)
    assert unknown.status.status_code is StatusCode.ERROR
    assert not trace.get_current_span().get_span_context().is_valid


@pytest.mark.parametrize(
    "tracing,metrics", [(False, True), (True, False), (False, False)]
)
async def test_signal_settings_are_independent(providers, tracing, metrics):
    tracer, meter, exporter, reader = providers
    channel = RpcChannel("demo")

    @channel.method()
    async def ping() -> None:
        pass

    server = channel.create_server(
        middleware=[
            OpenTelemetry(
                tracer_provider=tracer,
                meter_provider=meter,
                tracing=tracing,
                metrics=metrics,
            )
        ]
    )
    await server.handle({"jsonrpc": "2.0", "id": 1, "method": "demo.ping"})
    assert bool(exporter.get_finished_spans()) is tracing
    assert bool(points(reader, "rpckit.rpc.calls")) is metrics


async def test_excluded_connections_are_isolated(providers):
    tracer, meter, exporter, reader = providers
    channel = RpcChannel("media")

    @channel.stream()
    async def frames() -> AsyncIterator[bytes]:
        yield b"one"
        await asyncio.Event().wait()

    rpc = RpcService(
        middleware=[
            OpenTelemetry(
                tracer_provider=tracer,
                meter_provider=meter,
                exclude=lambda scope: scope.endpoint.path == "/excluded",
            )
        ]
    )
    rpc.stream("/excluded", frames)
    # Streams are mounted once; use a separate channel for the other connection.
    other = RpcChannel("other")

    @other.stream()
    async def other_frames() -> AsyncIterator[bytes]:
        yield b"two"
        await asyncio.Event().wait()

    rpc.stream("/included", other_frames)
    async with (
        RpcTestClient(rpc, "/excluded") as excluded,
        RpcTestClient(rpc, "/included") as included,
    ):
        assert await excluded.next_frame() == b"one"
        assert await included.next_frame() == b"two"
        assert total(reader, "rpckit.stream.frames") == 1
    assert [span.name for span in exporter.get_finished_spans()] == [
        "other.other_frames"
    ]


def test_fastapi_routes_add_real_rpc_spans_and_clean_up(providers):
    tracer, meter, exporter, reader = providers
    channel = RpcChannel("demo")

    @channel.method()
    async def ping() -> str:
        return "pong"

    rpc = RpcService()
    endpoint = rpc.socket("/rpc", channels=[channel])
    router = APIRouter()
    RpcRoutes(
        router, middleware=[OpenTelemetry(tracer_provider=tracer, meter_provider=meter)]
    ).mount(endpoint)
    web = FastAPI()
    web.include_router(router)
    with TestClient(web) as client, client.websocket_connect("/rpc") as socket:
        socket.send_json({"jsonrpc": "2.0", "id": 1, "method": "demo.ping"})
        assert socket.receive_json()["result"] == "pong"
    spans = [span for span in exporter.get_finished_spans() if span.name == "demo.ping"]
    assert len(spans) == 1
    assert total(reader, "rpckit.connections.active") == 0


def test_base_and_integrations_imports_do_not_require_optional_dependencies():
    script = """
import sys
import fastapi

# Required FastAPI versions may have their own OTel dependency. Isolate the
# RPC integration check after framework startup, then block fresh OTel imports.
for name in list(sys.modules):
    if name == "opentelemetry" or name.startswith("opentelemetry."):
        del sys.modules[name]

class Block:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".", 1)[0] in {"opentelemetry"}:
            raise ModuleNotFoundError("blocked", name=fullname)
sys.meta_path.insert(0, Block())
import fastapi_canon.integrations.websockets
import fastapi_canon.integrations.websockets.adapters
assert fastapi_canon.integrations.websockets.RpcMiddleware
try:
    import fastapi_canon.integrations.websockets.adapters.opentelemetry
except ModuleNotFoundError as error:
    assert "fastapi-canon[otel]" in str(error)
else:
    raise AssertionError("optional integration should fail without OTEL")
"""
    subprocess.run([sys.executable, "-c", script], check=True)


async def test_metrics_work_with_trace_sampling_disabled(providers):
    _, meter, _, reader = providers
    tracer = TracerProvider(sampler=ALWAYS_OFF)
    channel = RpcChannel("media")

    @channel.stream()
    async def frames() -> AsyncIterator[bytes]:
        yield b"one"

    rpc = RpcService(
        middleware=[OpenTelemetry(tracer_provider=tracer, meter_provider=meter)]
    )
    endpoint = rpc.stream("/media", frames)
    socket = InMemorySocket("/media")
    try:
        await endpoint.serve(socket)
        assert total(reader, "rpckit.stream.frames") == 1
    finally:
        tracer.shutdown()


async def test_subscription_control_calls_are_instrumented(providers):
    tracer, meter, exporter, reader = providers
    channel = RpcChannel("updates")

    @channel.subscription()
    async def values() -> AsyncIterator[int]:
        yield 1
        await asyncio.Event().wait()

    rpc = RpcService(
        middleware=[OpenTelemetry(tracer_provider=tracer, meter_provider=meter)]
    )
    rpc.socket("/rpc", channels=[channel])
    async with RpcTestClient(rpc, "/rpc") as client:
        subscription = await client.request("updates.values.subscribe")
        await client.next_notification()
        await client.request("updates.values.unsubscribe", subscription)
        with pytest.raises(RpcTestError):
            await client.request("updates.values.unsubscribe", subscription)
    assert [span.name for span in exporter.get_finished_spans()] == [
        "updates.values.subscribe",
        "updates.values.unsubscribe",
        "updates.values.unsubscribe",
    ]
    assert total(reader, "rpckit.rpc.calls") == 3
    assert total(reader, "rpckit.rpc.calls", **{"rpckit.outcome": "error"}) == 1
    assert total(reader, "rpckit.notifications") == 1


async def test_request_exclusion_and_optional_connection_span(providers):
    tracer, meter, exporter, reader = providers
    channel = RpcChannel("demo")

    @channel.method()
    async def ping() -> str:
        return "pong"

    instrumentation = OpenTelemetry(
        tracer_provider=tracer,
        meter_provider=meter,
        connection_spans=True,
        exclude=lambda scope: (
            isinstance(scope, RpcRequestScope) and scope.request.request_id == 1
        ),
    )
    rpc = RpcService(middleware=[instrumentation])
    rpc.socket("/rpc", channels=[channel])
    async with RpcTestClient(rpc, "/rpc") as client:
        assert await client.request("demo.ping") == "pong"
        assert await client.request("demo.ping") == "pong"
    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert set(spans) == {"WS /rpc", "demo.ping"}
    assert spans["demo.ping"].parent.span_id == spans["WS /rpc"].context.span_id
    assert total(reader, "rpckit.rpc.calls") == 1


@pytest.mark.parametrize("failed_send", [False, True])
async def test_failed_sends_and_invalid_input_are_not_counted(providers, failed_send):
    tracer, meter, exporter, reader = providers
    channel = RpcChannel("media")

    @channel.stream()
    async def frames(output: Inject[RpcBinaryOutput]) -> None:
        if failed_send:
            await output.send(b"unused")
        await asyncio.Event().wait()

    class Socket(InMemorySocket):
        async def send_bytes(self, data):
            raise RuntimeError("send failed")

    rpc = RpcService(
        middleware=[OpenTelemetry(tracer_provider=tracer, meter_provider=meter)]
    )
    endpoint = rpc.stream("/media", frames, limits=RpcLimits(max_message_bytes=2))
    socket = Socket("/media")
    if not failed_send:
        await socket.client_send(b"too large")
    await endpoint.serve(socket)
    assert total(reader, "rpckit.stream.frames") == 0
    assert total(reader, "rpckit.streams.active") == 0
    assert total(reader, "rpckit.connections.active") == 0
    assert socket.closed[0] is (
        RpcConnectionClose.INTERNAL_ERROR
        if failed_send
        else RpcConnectionClose.PROTOCOL_ERROR
    )
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR


def test_custom_handlers_measure_live_connections_and_accepted_duration(providers):
    tracer, meter, exporter, reader = providers
    endpoint = RpcService().socket("/rpc", channels=[RpcChannel("demo")])

    async def handler(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text("ready")
        await websocket.receive_text()
        await websocket.close()

    router = APIRouter()
    RpcRoutes(
        router,
        middleware=[
            OpenTelemetry(
                tracer_provider=tracer, meter_provider=meter, connection_spans=True
            )
        ],
    ).mount(endpoint, handler=handler)
    web = FastAPI()
    web.include_router(router)
    with TestClient(web) as client, client.websocket_connect("/rpc") as socket:
        assert socket.receive_text() == "ready"
        assert total(reader, "rpckit.connections.active") == 1
        socket.send_text("close")
        assert socket.receive()["code"] == 1000
    assert total(reader, "rpckit.connections.active") == 0
    assert total(reader, "rpckit.connections", **{"rpckit.outcome": "accepted"}) == 1
    assert total(reader, "rpckit.connections", **{"rpckit.outcome": "failed"}) == 0
    (duration,) = points(reader, "rpckit.connections.duration")
    assert duration.count == 1
    assert duration.sum > 0
    assert duration.attributes["rpckit.close"] == "normal"
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.UNSET


def test_delegated_middleware_records_rpc_and_connection_once(providers):
    tracer, meter, exporter, reader = providers
    telemetry = OpenTelemetry(tracer_provider=tracer, meter_provider=meter)
    channel = RpcChannel("demo")

    @channel.method()
    async def ping() -> str:
        return "pong"

    endpoint = RpcService().socket("/rpc", channels=[channel])

    async def handler(websocket: WebSocket) -> None:
        await serve_websocket(endpoint, websocket, middleware=[telemetry])

    router = APIRouter()
    RpcRoutes(router).mount(endpoint, handler=handler)
    web = FastAPI()
    web.include_router(router)
    with TestClient(web) as client, client.websocket_connect("/rpc") as socket:
        socket.send_json({"jsonrpc": "2.0", "id": 1, "method": "demo.ping"})
        assert socket.receive_json()["result"] == "pong"
        assert total(reader, "rpckit.connections.active") == 1
    assert [span.name for span in exporter.get_finished_spans()] == ["demo.ping"]
    assert total(reader, "rpckit.rpc.calls") == 1
    assert total(reader, "rpckit.rpc.active") == 0
    assert total(reader, "rpckit.connections") == 1
    assert total(reader, "rpckit.connections.active") == 0


def test_custom_handler_peer_disconnect_keeps_connection_span_successful(providers):
    tracer, meter, exporter, reader = providers
    endpoint = RpcService().socket("/rpc", channels=[RpcChannel("demo")])

    async def handler(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text("ready")
        await websocket.receive_text()

    router = APIRouter()
    RpcRoutes(
        router,
        middleware=[
            OpenTelemetry(
                tracer_provider=tracer, meter_provider=meter, connection_spans=True
            )
        ],
    ).mount(endpoint, handler=handler)
    web = FastAPI()
    web.include_router(router)
    with (
        TestClient(web) as client,
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/rpc") as socket,
    ):
        assert socket.receive_text() == "ready"
        socket.close(1000)
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.UNSET
    assert total(reader, "rpckit.connections") == 1
    assert total(reader, "rpckit.connections.active") == 0
