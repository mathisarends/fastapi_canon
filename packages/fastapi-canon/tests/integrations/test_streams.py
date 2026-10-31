"""Exercise the real optional dependency, including its contract/client tools."""

import asyncio
import copy
import importlib
import json
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from dishka import Provider, Scope, provide
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Send
from starlette.types import Scope as ASGIScope

pytest.importorskip("fastapi_canon.integrations.sse")

from examples.streams.app import (
    RunCompleted,
    RunEvent,
    RunNotFound,
    RunProvider,
    RunService,
    app,
    create_app,
    feature,
    router,
    run_errors,
    run_not_found,
)
from fastapi_canon import (
    CanonRouter,
    Composition,
    Error,
    ErrorConfigurationError,
    ErrorRegistry,
    Feature,
    HTTPMethod,
    RouterPolicy,
)
from fastapi_canon.integrations.sse import CanonStreamRouter, StreamRouter

AUTH = {"Authorization": "Bearer demo"}


@pytest.fixture
def stream_app() -> FastAPI:
    return create_app()


def test_router_combines_native_bases_and_feature_discovers_registry() -> None:
    assert isinstance(router, CanonRouter)
    assert isinstance(router, StreamRouter)
    assert feature.errors is not None
    assert feature.errors.errors == run_errors.errors


def test_shared_and_local_raises_are_deduplicated_with_shared_status() -> None:
    other = Error(KeyError, status=404, code="other_missing", title="Other missing")
    errors = ErrorRegistry(
        errors=[run_not_found, other], type_base="https://example.test/problems"
    )
    combined = CanonStreamRouter(
        error_registry=errors, raises=[run_not_found, run_not_found]
    )

    @combined.stream("/events", raises=[run_not_found, other, other])
    async def events() -> AsyncIterator[RunEvent]:
        yield RunCompleted(run_id=1)

    application = Composition(Feature(name="events", routers=[combined])).apply(
        FastAPI()
    )
    schema = application.openapi()
    response = schema["paths"]["/events"]["get"]["responses"]["404"]
    problem = response["content"]["application/problem+json"]["schema"]
    assert len(problem["oneOf"]) == 2
    assert set(problem["discriminator"]["mapping"]) == {
        "run_not_found",
        "other_missing",
    }


@pytest.mark.parametrize("scope", ["router", "operation"])
@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"raises": [run_not_found]}, "requires an error registry"),
        ({"error_registry": object()}, "error_registry must be"),
        ({"error_registry": run_errors, "raises": "invalid"}, "must be a sequence"),
        ({"error_registry": run_errors, "raises": [object()]}, "Error instance"),
        (
            {"error_registry": ErrorRegistry(errors=[]), "raises": [run_not_found]},
            "does not belong",
        ),
    ],
)
def test_invalid_canon_declarations(
    scope: str, options: dict[str, Any], message: str
) -> None:
    with pytest.raises(ErrorConfigurationError, match=message):
        if scope == "router":
            CanonStreamRouter(**options)
        else:
            selected = CanonStreamRouter(error_registry=options.get("error_registry"))

            @selected.stream("/events", raises=options.get("raises", ()))
            async def events() -> AsyncIterator[RunEvent]:
                yield RunCompleted(run_id=1)


@pytest.mark.parametrize("method", [HTTPMethod.GET, HTTPMethod.POST])
def test_domain_errors_from_dependencies_are_problem_responses(
    method: HTTPMethod, stream_app: FastAPI
) -> None:
    with TestClient(stream_app) as client:
        unauthenticated = client.request(method, "/runs/1/events", json={"steps": 1})
        missing = client.request(
            method, "/runs/0/events", headers=AUTH, json={"steps": 1}
        )
    for response, status, code in (
        (unauthenticated, 401, "authentication_required"),
        (missing, 404, "run_not_found"),
    ):
        assert response.status_code == status
        assert response.headers["content-type"] == "application/problem+json"
        assert response.json()["status"] == status
        assert response.json()["code"] == code
        assert response.json()["type"] == f"https://example.test/problems/{code}"


def test_http_exception_and_request_validation_are_normalized(
    stream_app: FastAPI,
) -> None:
    with TestClient(stream_app) as client:
        denied = client.get(
            "/runs/1/events", headers={"Authorization": "Bearer forbidden"}
        )
        invalid_path = client.get("/runs/invalid/events", headers=AUTH)
        invalid_body = client.post(
            "/runs/1/events", headers=AUTH, json={"steps": "private input"}
        )
    assert denied.status_code == 403
    assert denied.json()["code"] == "http_403"
    assert denied.json()["detail"] == "Access denied"
    for response in (denied, invalid_path, invalid_body):
        assert response.headers["content-type"] == "application/problem+json"
    for response in (invalid_path, invalid_body):
        assert response.status_code == 422
        assert response.json()["code"] == "request_validation_error"
        assert "private input" not in response.text


@pytest.mark.parametrize("method", [HTTPMethod.GET, HTTPMethod.POST])
def test_successful_typed_streams(method: HTTPMethod, stream_app: FastAPI) -> None:
    with TestClient(stream_app) as client:
        response = client.request(
            method, "/runs/7/events", headers=AUTH, json={"steps": 2}
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert events[-1] == {"type": "run.completed", "run_id": 7}
    assert events[0] == {"type": "run.progress", "step": 1}
    assert len(events) == (2 if method == "GET" else 3)


@pytest.mark.parametrize("with_errors", [False, True])
def test_sync_generators_and_composition_router_factory(with_errors: bool) -> None:
    sync = CanonStreamRouter(
        error_registry=run_errors if with_errors else None,
        raises=[run_not_found] if with_errors else (),
    )

    @sync.stream("/sync")
    def events() -> Iterator[RunEvent]:
        yield RunCompleted(run_id=2)

    application = Composition(
        Feature(name="sync", routers=[sync]),
        router_factory=lambda: CanonRouter(prefix="/api"),
    ).apply(FastAPI())
    with TestClient(application) as client:
        assert '"run_id":2' in client.get("/api/sync").text
    responses = application.openapi()["paths"]["/api/sync"]["get"]["responses"]
    assert ("404" in responses) is with_errors


def test_feature_discovers_stream_and_rest_registries_together() -> None:
    class OtherMissing(Exception):
        pass

    other_missing = Error(
        OtherMissing, status=404, code="other_missing", title="Other missing"
    )
    rest_errors = ErrorRegistry(
        errors=[other_missing], type_base="https://example.test/problems"
    )
    rest = CanonRouter(error_registry=rest_errors)

    @rest.get("/rest", raises=[other_missing])
    async def missing() -> None:
        raise OtherMissing

    mixed = Feature(name="mixed", routers=[router, rest], providers=[RunProvider])
    assert mixed.errors is not None
    assert mixed.errors.errors == (*run_errors.errors, other_missing)
    application = Composition(mixed).apply(FastAPI())
    with TestClient(application) as client:
        assert client.get("/rest").json()["code"] == "other_missing"
        assert (
            client.get("/runs/0/events", headers=AUTH).json()["code"] == "run_not_found"
        )
    assert "OtherMissingProblem" in application.openapi()["components"]["schemas"]


@pytest.mark.parametrize(
    ("options", "exception"),
    [
        ({"methods": [HTTPMethod.DELETE]}, ValueError),
        ({"response_model": dict[str, str]}, TypeError),
        ({"response_model_exclude_none": True}, TypeError),
        ({"status_code": 204}, ValueError),
    ],
)
def test_native_stream_contract_validation_is_preserved(
    options: dict[str, Any], exception: type[Exception]
) -> None:
    with pytest.raises(exception):
        CanonStreamRouter().stream("/events", **options)


def test_native_stream_endpoint_must_be_a_typed_generator() -> None:
    with pytest.raises(TypeError, match="generator"):

        @CanonStreamRouter().stream("/events")
        async def invalid() -> RunCompleted:
            return RunCompleted(run_id=1)


def test_http_method_enum_supports_multiple_native_stream_methods() -> None:
    selected = CanonStreamRouter()

    @selected.stream("/events", methods=[HTTPMethod.GET, HTTPMethod.POST])
    async def events() -> AsyncIterator[RunEvent]:
        yield RunCompleted(run_id=1)

    application = FastAPI()
    application.include_router(selected)
    with TestClient(application) as client:
        for method in (HTTPMethod.GET, HTTPMethod.POST):
            response = client.request(method, "/events")
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
        assert client.put("/events").status_code == 405


@pytest.mark.parametrize("method", [HTTPMethod.GET, HTTPMethod.POST])
def test_dishka_request_service_spans_preconditions_and_stream(
    method: HTTPMethod,
) -> None:
    calls: list[str] = []

    class TrackedRunService(RunService):
        closed = False

        async def require_existing(self, run_id: int) -> None:
            assert not self.closed
            calls.append("check")
            await super().require_existing(run_id)

        async def events(
            self, run_id: int, *, steps: int = 1
        ) -> AsyncIterator[RunEvent]:
            assert not self.closed
            calls.append("stream")
            async for event in super().events(run_id, steps=steps):
                yield event
            assert not self.closed
            calls.append("stream done")

    class TrackedRunProvider(Provider):
        @provide(scope=Scope.REQUEST)
        async def run_service(self) -> AsyncIterator[RunService]:
            service = TrackedRunService()
            calls.append("service open")
            try:
                yield service
            finally:
                service.closed = True
                calls.append("service close")

    application = Composition(
        Feature(name="tracked", routers=[router], providers=[TrackedRunProvider])
    ).apply(FastAPI())

    async def recorded_app(scope: ASGIScope, receive: Receive, send: Send) -> None:
        async def record_start(message: Message) -> None:
            if message["type"] == "http.response.start":
                calls.append(f"HTTP {message['status']}")
            await send(message)

        await application(scope, receive, record_start)

    with TestClient(recorded_app) as client:
        success = client.request(
            method, "/runs/1/events", headers=AUTH, json={"steps": 2}
        )
        assert success.status_code == 200
        assert calls[:2] == ["service open", "check"]
        assert calls.count("service open") == 1
        assert calls.index("check") < calls.index("HTTP 200")
        assert calls.index("check") < calls.index("stream")
        assert calls[-2:] == ["stream done", "service close"]
        calls.clear()
        missing = client.request(
            method, "/runs/0/events", headers=AUTH, json={"steps": 2}
        )
        assert missing.status_code == 404
        assert missing.headers["content-type"] == "application/problem+json"
        assert missing.json()["code"] == "run_not_found"
        assert calls == ["service open", "check", "HTTP 404", "service close"]


def test_compilation_preserves_entire_native_sse_contract() -> None:
    native = StreamRouter()

    @native.stream("/{run_id}")
    async def events(run_id: int) -> AsyncIterator[RunEvent]:
        yield RunCompleted(run_id=run_id)

    application = FastAPI()
    application.include_router(native)
    expected = application.openapi()["paths"]["/{run_id}"]["get"]["responses"]["200"]
    schema = app.openapi()
    for method in ("get", "post"):
        responses = schema["paths"]["/runs/{run_id}/events"][method]["responses"]
        assert responses["200"] == expected
        media = responses["200"]["content"]["text/event-stream"]
        payload = media["itemSchema"]["properties"]["data"]["contentSchema"]
        if "$ref" in payload:
            payload = schema["components"]["schemas"][payload["$ref"].rsplit("/", 1)[1]]
        assert len(payload["oneOf"]) == 2
        assert payload["discriminator"]["propertyName"] == "type"
        assert set(responses) >= {"200", "401", "403", "404", "422"}
        for status in ("401", "403", "404", "422"):
            assert set(responses[status]["content"]) == {"application/problem+json"}
    assert "RunCompleted" in schema["components"]["schemas"]
    for name, definition in application.openapi()["components"]["schemas"].items():
        assert schema["components"]["schemas"][name] == definition
    assert "RunNotFoundProblem" in schema["components"]["schemas"]
    assert "x-fastapi-canon" not in json.dumps(schema)


def test_composition_policy_preserves_typed_sse_and_runs_before_streaming() -> None:
    selected = CanonStreamRouter(error_registry=run_errors)

    @selected.stream("/policy-events", raises=[run_not_found])
    async def events() -> AsyncIterator[RunEvent]:
        yield RunCompleted(run_id=1)

    baseline = Composition(Feature(name="baseline", routers=[selected])).apply(
        FastAPI()
    )
    expected = baseline.openapi()["paths"]["/policy-events"]["get"]["responses"]["200"]

    def require_run() -> None:
        raise RunNotFound(1)

    policy = RouterPolicy(
        error_registry=run_errors,
        dependencies=[Depends(require_run)],
        raises=[run_not_found],
    )
    application = Composition(
        Feature(name="events", routers=[selected]), router_policy=policy
    ).apply(FastAPI())
    responses = application.openapi()["paths"]["/policy-events"]["get"]["responses"]
    assert responses["200"] == expected
    assert responses["404"]["content"]["application/problem+json"]["schema"] == {
        "$ref": "#/components/schemas/RunNotFoundProblem"
    }
    response = TestClient(application).get("/policy-events")
    assert response.status_code == 404
    assert response.json()["code"] == "run_not_found"


def test_generator_body_errors_happen_after_response_start() -> None:
    selected = CanonStreamRouter(error_registry=run_errors)
    calls: list[str] = []

    async def before_start() -> None:
        calls.append("dependency")

    @selected.stream(
        "/late/{run_id}", raises=[run_not_found], dependencies=[Depends(before_start)]
    )
    async def late(run_id: int) -> AsyncIterator[RunEvent]:
        calls.append("generator")
        if run_id == 0:
            raise RunNotFound
        yield RunCompleted(run_id=run_id)

    application = Composition(Feature(name="late", routers=[selected])).apply(FastAPI())
    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.get("/late/0")
    assert calls == ["dependency", "generator"]
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "application/problem+json" not in response.text


@pytest.mark.parametrize("language", ["python", "typescript"])
def test_export_and_generate_both_clients(tmp_path: Path, language: str) -> None:
    pytest.importorskip("jinja2")
    from fastapi_canon.integrations.sse.codegen.cli import main
    from fastapi_canon.integrations.sse.codegen.openapi import export_schema

    cached = copy.deepcopy(app.openapi())
    document = export_schema(app)
    assert document["openapi"] == "3.2.0"
    assert app.openapi() == cached
    schema_path = tmp_path / "openapi.json"
    output = tmp_path / "sdk"
    assert (
        main(
            [
                "schema",
                "--app",
                "examples.streams.app:app",
                "--output",
                str(schema_path),
            ]
        )
        == 0
    )
    assert json.loads(schema_path.read_text()) == document
    args = [
        "--schema",
        str(schema_path),
        "--language",
        language,
        "--output",
        str(output),
    ]
    assert main(["generate", *args]) == 0
    assert main(["check", "--app", "examples.streams.app:app", *args]) == 0
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in output.rglob("*") if path.is_file()
    )
    for name in (
        "RunCompleted",
        "RunNotFoundProblem",
        "AuthenticationRequiredProblem",
        "RequestValidationProblem",
        "Http403Problem",
    ):
        assert name in source
    if language == "python":
        sys.path.insert(0, str(tmp_path))
        try:
            sdk = importlib.import_module("sdk")
            models = importlib.import_module("sdk.models")
            event = models.RunCompleted(type="run.completed", run_id=1)
            assert event.run_id == 1

            async def consume() -> None:
                application = create_app()
                transport = httpx.ASGITransport(app=application)
                async with (
                    application.router.lifespan_context(application),
                    httpx.AsyncClient(
                        transport=transport, base_url="http://test"
                    ) as http,
                    sdk.Client("http://test", http_client=http) as client,
                ):
                    received = [
                        event
                        async for event in client.runs.run_events(
                            run_id=7, authorization="Bearer demo"
                        )
                    ]
                    assert received[-1].run_id == 7
                    posted = [
                        event
                        async for event in client.runs.create_run_events(
                            run_id=7,
                            authorization="Bearer demo",
                            body=models.RunRequest(steps=2),
                        )
                    ]
                    assert len(posted) == 3
                    assert posted[-1].run_id == 7
                    with pytest.raises(sdk.HttpError) as raised:
                        _ = [
                            event
                            async for event in client.runs.run_events(
                                run_id=0, authorization="Bearer demo"
                            )
                        ]
                    assert raised.value.status == 404
                    problem = models.RunNotFoundProblem.model_validate(
                        raised.value.details
                    )
                    assert problem.code == "run_not_found"

            asyncio.run(consume())
        finally:
            sys.path.remove(str(tmp_path))
            for name in tuple(sys.modules):
                if name == "sdk" or name.startswith("sdk."):
                    del sys.modules[name]
