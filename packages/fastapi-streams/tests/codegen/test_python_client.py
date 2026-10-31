import asyncio
import importlib.util
import sys
from enum import StrEnum

import httpx
import pytest
from fastapi_canon.integrations.sse.codegen.cli import main
from fastapi_canon.integrations.sse.codegen.ir import build_ir
from fastapi_canon.integrations.sse.codegen.openapi import export_schema
from fastapi_canon.integrations.sse.codegen.python import generate_python
from pydantic import TypeAdapter

from examples.agent_runs.generated_python import (
    Client,
    HttpError,
    RequestOptions,
    RunEventType,
    StreamParseError,
    TransportError,
)
from examples.agent_runs.generated_python.models import RunEvent, RunRequest
from examples.agent_runs.generated_python.runtime import iter_sse


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], *, stall: bool = False):
        self.chunks = chunks
        self.stall = stall
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.stall:
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


class FailingChunks(Chunks):
    async def __aiter__(self):
        async for chunk in super().__aiter__():
            yield chunk
        raise httpx.ReadError("connection lost")


@pytest.mark.parametrize("chunks", [[], [b'{"detail":']])
async def test_failed_error_body_preserves_status_response_and_cause(chunks):
    source = FailingChunks(chunks)
    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    503, headers={"X-Error": "upstream"}, stream=source
                )
            )
        ) as http,
        Client("http://example.test", http_client=http) as client,
    ):
        with pytest.raises(HttpError) as raised:
            _ = [
                event async for event in client.agent_runs.stream_run_events(run_id="x")
            ]
        assert raised.value.status == 503
        assert raised.value.response.headers["X-Error"] == "upstream"
        assert isinstance(raised.value.__cause__, httpx.ReadError)
        assert raised.value.details == ('{"detail":' if chunks else None)
        assert source.closed


@pytest.mark.parametrize(
    ("payload", "details"),
    [
        (b'{"detail":"denied"}', {"detail": "denied"}),
        (b"proxy failed", "proxy failed"),
        (b"", None),
    ],
)
async def test_http_errors_keep_json_text_empty_details_and_close(payload, details):
    source = Chunks([payload])
    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(403, stream=source))
        ) as http,
        Client("http://example.test", http_client=http) as client,
    ):
        with pytest.raises(HttpError) as raised:
            _ = [
                event async for event in client.agent_runs.stream_run_events(run_id="x")
            ]
        assert raised.value.details == details
        assert raised.value.status == 403
        assert source.closed


async def test_error_diagnostics_stop_at_64_kib_and_close():
    source = FailingChunks([b"x" * 100_000])
    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(502, stream=source))
        ) as http,
        Client("http://example.test", http_client=http) as client,
    ):
        with pytest.raises(HttpError) as raised:
            _ = [
                event async for event in client.agent_runs.stream_run_events(run_id="x")
            ]
        assert raised.value.details == "x" * 65536
        assert raised.value.__cause__ is None
        assert source.closed


@pytest.mark.parametrize(
    ("media", "payload", "message"),
    [
        ("text/html", b"html", "Expected text/event-stream"),
        ("", b"", "Expected text/event-stream"),
        ("text/event-stream", b"\xff", "Invalid UTF-8"),
        ("text/event-stream", b"data: \xe2", "Invalid UTF-8"),
    ],
)
async def test_transport_failures_close_responses(media, payload, message):
    source = Chunks([payload])
    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, headers={"Content-Type": media}, stream=source
                )
            )
        ) as http,
        Client("http://example.test", http_client=http) as client,
    ):
        with pytest.raises(TransportError, match=message):
            _ = [
                event async for event in client.agent_runs.stream_run_events(run_id="x")
            ]
        assert source.closed


async def test_connect_failure_retains_original_cause():
    def handler(request):
        raise httpx.ConnectError("offline", request=request)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http,
        Client("http://example.test", http_client=http) as client,
    ):
        with pytest.raises(TransportError) as raised:
            _ = [
                event async for event in client.agent_runs.stream_run_events(run_id="x")
            ]
        assert isinstance(raised.value.__cause__, httpx.ConnectError)


async def test_read_failure_after_an_event_closes_response():
    source = FailingChunks([b'data: {"type":"run.completed","run_id":"ok"}\n\n'])
    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, headers={"Content-Type": "text/event-stream"}, stream=source
                )
            )
        ) as http,
        Client("http://example.test", http_client=http) as client,
    ):
        events = client.agent_runs.stream_run_events(run_id="x")
        assert (await anext(events)).run_id == "ok"
        with pytest.raises(TransportError) as raised:
            await anext(events)
        assert isinstance(raised.value.__cause__, httpx.ReadError)
        assert source.closed


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
async def test_python_parser_line_endings_metadata_and_eof(newline):
    payload = newline.join(
        [": ping", "", "event: ignored", "id: 1", "", "data: 1", "", "data: 2"]
    )
    response = httpx.Response(200, stream=Chunks([payload.encode()]))
    assert [item async for item in iter_sse(response)] == ["1", "2"]


async def test_sdk_owned_client_is_closed_by_context_manager():
    async with Client("http://example.test") as client:
        http = client._transport.http_client
        assert not http.is_closed
    assert http.is_closed


def test_optional_literal_defaults_produce_valid_discriminator_models(tmp_path):
    from examples.agent_runs import app

    document = export_schema(app)
    started = document["components"]["schemas"]["RunStarted"]
    started["required"].remove("type")
    started["properties"]["type"]["default"] = "run.started"
    files = generate_python(build_ir(document))
    model_file = tmp_path / "models.py"
    model_file.write_text(files["models.py"], encoding="utf-8")
    spec = importlib.util.spec_from_file_location(
        "generated_default_models", model_file
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        event = module.RunStarted(run_id="default")
        assert event.type is module.RunEventType.RUN_STARTED
        assert event.model_dump(exclude_unset=True) == {"run_id": "default"}
        parsed = TypeAdapter(module.RunEvent).validate_json(
            '{"type":"run.started","run_id":"ok"}'
        )
        assert parsed.type is module.RunEventType.RUN_STARTED
    finally:
        sys.modules.pop(spec.name)


async def test_python_parser_all_boundaries_and_multiline_eof():
    raw = (
        '\ufeff: ping\r\n\r\ndata: {\r\ndata: "text":"世界 👋"}\r\n\r\ndata: 2'.encode()
    )
    for cut in range(len(raw) + 1):
        response = httpx.Response(200, stream=Chunks([raw[:cut], raw[cut:]]))
        assert [data async for data in iter_sse(response)] == [
            '{\n"text":"世界 👋"}',
            "2",
        ]
    response = httpx.Response(200, stream=Chunks([bytes([value]) for value in raw]))
    assert len([data async for data in iter_sse(response)]) == 2


async def test_python_generated_requests_models_errors_and_client_ownership():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.headers.get("Authorization") != "Bearer token":
            return httpx.Response(403, json={"detail": "Denied"})
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=Chunks([b'data: {"type":"run.completed","run_id":"ok"}\n\n']),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with Client(
            "http://example.test/",
            headers={"Authorization": "Bearer token"},
            http_client=http,
        ) as client:
            events = [
                event
                async for event in client.agent_runs.stream_run_events(
                    run_id="a/b ?", delay=0
                )
            ]
            assert events[0].type == "run.completed"
            assert isinstance(events[0].type, StrEnum)
            assert events[0].type is RunEventType.RUN_COMPLETED
            assert events[0].run_id == "ok"
            assert requests[-1].url.raw_path == b"/v1/runs/a%2Fb%20%3F/events?delay=0"
            events = [
                event
                async for event in client.agent_runs.stream_prompt(
                    run_id="ok", body=RunRequest(prompt="Hello")
                )
            ]
            assert events[0].type == "run.completed"
            assert requests[-1].method == "POST"
            assert requests[-1].content == b'{"prompt":"Hello"}'
        assert not http.is_closed
        async with Client("http://example.test", http_client=http) as client:
            with pytest.raises(HttpError) as raised:
                _ = [
                    event
                    async for event in client.agent_runs.stream_run_events(run_id="x")
                ]
            assert raised.value.status == 403
            assert raised.value.details == {"detail": "Denied"}


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        (None, 0.25),
        (RequestOptions(), 0.25),
        (RequestOptions(headers={"X-Test": "yes"}), 0.25),
        (RequestOptions(timeout=None), None),
        (RequestOptions(timeout=0.5), 0.5),
    ],
)
async def test_request_timeouts_inherit_override_or_disable(options, expected):
    def handler(request):
        assert request.extensions["timeout"] == dict.fromkeys(
            ("connect", "read", "write", "pool"), expected
        )
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content='data: {"type":"run.completed","run_id":"ok"}\n\n',
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=0.25) as http,
        Client("http://example.test", http_client=http) as client,
    ):
        assert (
            len(
                [
                    event
                    async for event in client.agent_runs.stream_run_events(
                        run_id="ok", options=options
                    )
                ]
            )
            == 1
        )


async def test_python_stream_close_cancellation_and_validation():
    for payload in (
        b"broken",
        b'{"type":"unknown"}',
        b'{"type":"token.delta","text":1}',
    ):
        source = Chunks([b"data: " + payload + b"\n\n"])
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _, source=source: httpx.Response(
                    200, headers={"Content-Type": "text/event-stream"}, stream=source
                )
            )
        ) as http:
            async with Client("http://example.test", http_client=http) as client:
                with pytest.raises(StreamParseError):
                    _ = [
                        event
                        async for event in client.agent_runs.stream_run_events(
                            run_id="x"
                        )
                    ]
            assert source.closed
    source = Chunks([b'data: {"type":"token.delta","text":"hello"}\n\n'], stall=True)
    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, headers={"Content-Type": "text/event-stream"}, stream=source
                )
            )
        ) as http,
        Client("http://example.test", http_client=http) as client,
    ):
        events = client.agent_runs.stream_run_events(run_id="x")
        assert (await anext(events)).type == "token.delta"
        await events.aclose()
        assert source.closed
        source.closed = False
        events = client.agent_runs.stream_run_events(run_id="x")
        await anext(events)
        task = asyncio.create_task(anext(events))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert source.closed


@pytest.mark.snapshot
def test_python_codegen_cli_and_snapshot(tmp_path):
    from examples.agent_runs import app

    contract = build_ir(export_schema(app))
    files = generate_python(contract)
    assert files == generate_python(contract)
    assert (
        main(
            [
                "check",
                "--schema",
                "tests/fixtures/agent_runs.openapi.json",
                "--language",
                "python",
                "--output",
                "examples/agent_runs/generated_python",
                "--strict",
            ]
        )
        == 0
    )
    schema = tmp_path / "schema.json"
    assert (
        main(["schema", "--app", "examples.agent_runs:app", "--output", str(schema)])
        == 0
    )
    args = [
        "generate",
        "--schema",
        str(schema),
        "--language",
        "python",
        "--output",
        str(tmp_path / "sdk"),
    ]
    assert main(args) == 0
    assert main(args + ["--check"]) == 0
    (tmp_path / "sdk/models.py").write_text("stale")
    assert main(args + ["--check"]) == 1
    assert (
        TypeAdapter(RunEvent)
        .validate_json('{"type":"token.delta","text":"typed"}')
        .text
        == "typed"
    )


async def test_sdk_owned_client_bounds_connect_but_not_stream_idle_time():
    async with Client("http://example.test") as client:
        timeout = client._transport.http_client.timeout
    assert timeout == httpx.Timeout(None, connect=10.0)
