from copy import deepcopy

import pytest
from fastapi_canon.integrations.sse.codegen.ir import build_ir
from fastapi_canon.integrations.sse.codegen.python import generate_python
from pydantic import ValidationError

from tests.codegen.helpers import event_document, load_package


def request_document(*, body=False):
    document = event_document(str)
    item = document["paths"].pop("/events")
    document["paths"]["/runs/{run_id}"] = item
    operation = item["get"]
    operation["operationId"] = "get_run_events"
    operation["parameters"] = [
        {
            "name": "run_id",
            "in": "path",
            "required": True,
            "schema": {"$ref": "#/components/schemas/RunId"},
        },
        {
            "name": "limit",
            "in": "query",
            "schema": {"type": "integer", "minimum": 2, "maximum": 10, "multipleOf": 2},
        },
        {
            "name": "search",
            "in": "query",
            "schema": {"type": "string", "minLength": 2, "maxLength": 12},
        },
        {
            "name": "scores",
            "in": "query",
            "explode": False,
            "schema": {
                "type": "array",
                "items": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "exclusiveMaximum": 5,
                },
            },
        },
        {
            "name": "X-Trace",
            "in": "header",
            "schema": {"type": "string", "pattern": "^trace_[a-z]+$"},
        },
    ]
    document["components"] = {
        "schemas": {
            "RunId": {"type": "string", "pattern": "^run_[A-Za-z0-9_-]+$"},
        }
    }
    if body:
        item["post"] = item.pop("get")
        operation["requestBody"] = {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["display-name"],
                        "properties": {
                            "display-name": {"type": "string", "minLength": 2}
                        },
                        "additionalProperties": False,
                    }
                }
            },
        }
    return document


@pytest.mark.parametrize("backend", ["httpx", "httpx2"])
@pytest.mark.parametrize(
    "arguments",
    [
        {"run_id": "invalid"},
        {"limit": 1},
        {"limit": 12},
        {"limit": 3},
        {"search": "x"},
        {"search": "x" * 13},
        {"scores": [0]},
        {"scores": [5]},
        {"x_trace": "bad"},
        {"run_id": None},
    ],
)
async def test_invalid_parameters_fail_at_call_without_transport(
    tmp_path, backend, arguments
):
    http = __import__(backend)
    requests = []
    with load_package(
        tmp_path / "sdk",
        generate_python(build_ir(request_document()), http_client=backend),
    ) as sdk:
        async with (
            http.AsyncClient(
                transport=http.MockTransport(lambda request: requests.append(request))
            ) as client,
            sdk.Client("http://example.test", http_client=client) as api,
        ):
            with pytest.raises(ValidationError):
                api.streams.get_run_events(**({"run_id": "run_ok"} | arguments))
            assert requests == []


@pytest.mark.parametrize("backend", ["httpx", "httpx2"])
async def test_valid_parameters_keep_serialization_and_lazy_http(tmp_path, backend):
    http = __import__(backend)
    requests = []

    def handler(request):
        requests.append(request)
        return http.Response(
            200, headers={"content-type": "text/event-stream"}, content='data: "ok"\n\n'
        )

    with load_package(
        tmp_path / "sdk",
        generate_python(build_ir(request_document()), http_client=backend),
    ) as sdk:
        async with (
            http.AsyncClient(transport=http.MockTransport(handler)) as client,
            sdk.Client("http://example.test", http_client=client) as api,
        ):
            scores = [1, 2.5]
            events = api.streams.get_run_events(
                run_id="run_ok",
                limit=4,
                search="a b/?",
                scores=scores,
                x_trace="trace_ok",
            )
            assert requests == []
            assert [event async for event in events] == ["ok"]
            request = requests[0]
            assert request.url.path == "/runs/run_ok"
            assert request.url.params.multi_items() == [
                ("limit", "4"),
                ("scores", "1,2.5"),
                ("search", "a b/?"),
            ]
            assert request.headers["X-Trace"] == "trace_ok"
            assert scores == [1, 2.5]
            assert [
                event async for event in api.streams.get_run_events(run_id="run_ok")
            ] == ["ok"]
            assert requests[-1].url.query == b""
            assert "X-Trace" not in requests[-1].headers
            await api.aclose()
            assert not client.is_closed


@pytest.mark.parametrize("backend", ["httpx", "httpx2"])
async def test_body_constraints_and_aliases_validate_at_call(tmp_path, backend):
    http = __import__(backend)
    requests = []

    def handler(request):
        requests.append(request)
        return http.Response(
            200, headers={"content-type": "text/event-stream"}, content='data: "ok"\n\n'
        )

    with load_package(
        tmp_path / "sdk",
        generate_python(build_ir(request_document(body=True)), http_client=backend),
    ) as sdk:
        async with (
            http.AsyncClient(transport=http.MockTransport(handler)) as client,
            sdk.Client("http://example.test", http_client=client) as api,
        ):
            for invalid in (
                {"display-name": "x"},
                {"display-name": "ok", "extra": 1},
            ):
                with pytest.raises(ValidationError):
                    api.streams.get_run_events(run_id="run_ok", body=invalid)
            assert requests == []
            events = api.streams.get_run_events(
                run_id="run_ok", body={"display-name": "ok"}
            )
            assert requests == []
            assert [event async for event in events] == ["ok"]
            assert requests[0].content == b'{"display-name":"ok"}'


@pytest.mark.parametrize("explode", [False, True])
async def test_query_array_explosion_and_path_encoding_are_preserved(tmp_path, explode):
    import httpx

    document = deepcopy(request_document())
    document["components"]["schemas"]["RunId"]["pattern"] = "^run_.+$"
    document["paths"]["/runs/{run_id}"]["get"]["parameters"][3]["explode"] = explode
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content='data: "ok"\n\n'
        )

    with load_package(tmp_path / "sdk", generate_python(build_ir(document))) as sdk:
        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client,
            sdk.Client("http://example.test", http_client=client) as api,
        ):
            assert (
                await anext(
                    api.streams.get_run_events(run_id="run_a/b", scores=[1, 2.0])
                )
                == "ok"
            )
            assert requests[0].url.raw_path.startswith(b"/runs/run_a%2Fb?")
            assert requests[0].url.params.multi_items() == (
                [("scores", "1"), ("scores", "2.0")]
                if explode
                else [("scores", "1,2.0")]
            )
