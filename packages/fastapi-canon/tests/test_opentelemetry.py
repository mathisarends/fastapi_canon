import asyncio
import subprocess
import sys
import textwrap
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import FrozenInstanceError
from uuid import UUID, uuid4

import pytest
from dishka import FromDishka, Provider, Scope, make_async_container, provide
from dishka.integrations.fastapi import inject
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import Histogram, InMemoryMetricReader
from opentelemetry.trace import SpanKind, StatusCode
from starlette.types import Scope as ASGIScope

import fastapi_canon.integrations.opentelemetry as telemetry_api
from fastapi_canon import (
    CanonRouter,
    Composition,
    ErrorOptions,
    Feature,
    FeatureConfigurationError,
)
from fastapi_canon.error import Error, ErrorRegistry
from fastapi_canon.integrations.opentelemetry import Telemetry, instrument
from fastapi_canon.testing.telemetry import SpanCapture


class Missing(Exception):
    pass


class Repository(ABC):
    @abstractmethod
    async def get(self, key: UUID, secret: str = "private", fail: str = "") -> str: ...

    @abstractmethod
    async def save(self, body: dict[str, str]) -> None: ...


class MemoryRepository(Repository):
    async def get(self, key: UUID, secret: str = "private", fail: str = "") -> str:
        if fail == "expected":
            raise Missing(secret)
        if fail == "unexpected":
            raise ValueError(secret)
        if fail == "cancel":
            raise asyncio.CancelledError()
        return str(key)

    async def save(self, body: dict[str, str]) -> None:
        pass


class Service:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    async def run(self, key: UUID, fail: str = "") -> str:
        return await self.repository.get(key, fail=fail)


class CallableServiceBase:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    async def __call__(self, key: UUID, fail: str = "") -> str:
        return await self.repository.get(key, fail=fail)


class CallableService(CallableServiceBase):
    pass


def callable_application(options: Telemetry) -> FastAPI:
    class Dependencies(Provider):
        repository = provide(MemoryRepository, provides=Repository, scope=Scope.REQUEST)
        service = provide(CallableService, scope=Scope.REQUEST)

    router = CanonRouter(
        error_registry=ErrorRegistry(
            type_base="https://example.test/problems",
            errors=[Error(Missing, status=404, code="missing", title="Missing")],
        )
    )

    @router.get("/callable/{key}")
    @inject
    async def get_item(
        key: UUID, service: FromDishka[CallableService], fail: str = ""
    ) -> str:
        return await service(key, fail=fail)

    return Composition(
        Feature(
            name="items",
            providers=[Dependencies],
            routers=[router],
            instrumentation=[
                instrument(Repository, expected=(Missing,)),
                instrument(CallableService, expected=(Missing,)),
            ],
        ),
        telemetry=options,
    ).apply(FastAPI())


def application(options: Telemetry | None, *, expected: bool = True) -> FastAPI:
    class Dependencies(Provider):
        @provide(scope=Scope.REQUEST)
        async def repository(self) -> AsyncIterator[Repository]:
            repository = MemoryRepository()
            yield repository
            closed.append(repository)

        service = provide(Service, scope=Scope.REQUEST)

    closed: list[Repository] = []
    errors = ErrorRegistry(
        type_base="https://example.test/problems",
        errors=[Error(Missing, status=404, code="missing", title="Missing")],
    )
    router = CanonRouter(error_registry=errors)

    @router.get("/items/{key}", raises=[errors.errors[0]])
    @inject
    async def get_item(key: UUID, service: FromDishka[Service], fail: str = "") -> str:
        return await service.run(key, fail)

    @router.get("/http")
    async def http_error() -> None:
        raise HTTPException(403, "private detail")

    @router.get("/broken")
    async def broken() -> None:
        raise RuntimeError("private error")

    feature = Feature(
        name="items",
        providers=[Dependencies],
        routers=[router],
        instrumentation=[
            instrument(Repository, expected=[Missing] if expected else []),
            instrument(Service, expected=[Missing] if expected else []),
        ],
    )
    composition = Composition(
        feature,
        telemetry=options,
        errors=ErrorOptions(type_base="https://example.test/problems"),
    )
    app = composition.apply(FastAPI())
    app.state.closed = closed
    composition.apply(app)
    composition.validate(app)
    return app


def test_trace_tree_attributes_parent_context_and_cleanup() -> None:
    global_provider = trace.get_tracer_provider()
    with SpanCapture() as capture:
        app = application(Telemetry(tracer_provider=capture.provider, logs=False))
        key = uuid4()
        parent = "00-12345678901234567890123456789012-1234567890123456-01"
        with TestClient(app) as client:
            assert (
                client.get(f"/items/{key}", headers={"traceparent": parent}).status_code
                == 200
            )
        capture.assert_tree(
            "GET /items/{key}",
            [
                "fastapi.dependencies",
                (
                    "fastapi.endpoint",
                    [
                        ("Service.run", ["Repository.get"]),
                    ],
                ),
                "fastapi.serialization",
            ],
        )
        server = capture.one("GET /items/{key}")
        assert server.kind is SpanKind.SERVER
        assert server.parent is not None and server.parent.span_id == int(
            "1234567890123456", 16
        )
        span = capture.one("Repository.get")
        assert span.kind is SpanKind.INTERNAL
        assert span.attributes is not None
        assert str(key) not in str(span.attributes)
        assert span.attributes["canon.adapter"] == "MemoryRepository"
        assert span.attributes["canon.feature"] == "items"
        assert all(
            "private" not in str(item.attributes) + str(item.events)
            for item in capture.spans
        )
        assert len(app.state.closed) == 1
        assert isinstance(app.state.closed[0], MemoryRepository)
    assert trace.get_tracer_provider() is global_provider


@pytest.mark.parametrize(
    ("fail", "status", "error", "port_status"),
    [
        ("expected", 404, "missing", StatusCode.UNSET),
        ("unexpected", 500, "internal_server_error", StatusCode.ERROR),
    ],
)
def test_expected_and_unexpected_failures(
    fail: str, status: int, error: str, port_status: StatusCode
) -> None:
    with SpanCapture() as capture:
        app = application(Telemetry(tracer_provider=capture.provider, logs=False))
        with TestClient(app, raise_server_exceptions=False) as client:
            assert client.get(f"/items/{uuid4()}?fail={fail}").status_code == status
        server = capture.one("GET /items/{key}")
        assert (
            server.attributes is not None
            and server.attributes["canon.problem.code"] == error
        )
        assert server.status.status_code is (
            StatusCode.ERROR if status == 500 else StatusCode.UNSET
        )
        assert ("error.type" in server.attributes) is (status >= 500)
        if status >= 500:
            assert server.attributes["error.type"] == error
        for name in ("Service.run", "Repository.get"):
            port = capture.one(name)
            assert port.status.status_code is port_status
            assert len(port.events) == 1
            assert port.events[0].name == (
                "canon.port.expected_exception" if fail == "expected" else "exception"
            )
            assert port.attributes is not None
            assert ("error.type" in port.attributes) is (fail != "expected")
            assert "private" not in str(port.events)


@pytest.mark.parametrize(
    ("path", "status", "code"),
    [
        ("/http", 403, "http_403"),
        ("/items/invalid", 422, "request_validation_error"),
        ("/broken", 500, "internal_server_error"),
    ],
)
def test_builtin_problem_outcomes(path: str, status: int, code: str) -> None:
    with SpanCapture() as capture:
        app = application(Telemetry(tracer_provider=capture.provider, logs=False))
        with TestClient(app, raise_server_exceptions=False) as client:
            assert client.get(path).status_code == status
        server = next(span for span in capture.spans if span.kind is SpanKind.SERVER)
        assert (
            server.attributes is not None
            and server.attributes["canon.problem.code"] == code
        )
        assert server.status.status_code is (
            StatusCode.ERROR if status == 500 else StatusCode.UNSET
        )
        assert ("error.type" in server.attributes) is (status >= 500)


def test_exclusion_and_operation_switch() -> None:
    with SpanCapture() as capture:
        calls: list[str] = []

        def exclude(scope: ASGIScope) -> bool:
            calls.append(str(scope["path"]))
            return str(scope["path"]).endswith("/http")

        app = application(
            Telemetry(
                tracer_provider=capture.provider, operation_spans=False, exclude=exclude
            )
        )
        with TestClient(app) as client:
            client.get("/http")
            assert not capture.spans
            client.get(f"/items/{uuid4()}")
        capture.assert_tree("GET /items/{key}", [("Service.run", ["Repository.get"])])
        assert len(calls) == 2
        assert not any(span.name.startswith("fastapi.") for span in capture.spans)


def test_metrics_only_no_arguments_in_metric_labels() -> None:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    with SpanCapture() as capture:
        app = application(
            Telemetry(
                tracer_provider=capture.provider,
                meter_provider=provider,
                tracing=False,
                port_metrics=True,
            )
        )
        with TestClient(app) as client:
            client.get(f"/items/{uuid4()}")
        assert not capture.spans
    data = reader.get_metrics_data()
    assert data is not None
    metric = next(
        metric
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == "canon.port.duration"
    )
    assert metric.unit == "s"
    assert isinstance(metric.data, Histogram)
    for point in metric.data.data_points:
        assert point.attributes is not None
        assert set(point.attributes) == {
            "canon.feature",
            "canon.port",
            "code.function.name",
        }
        assert point.count == 1
    provider.shutdown()


def test_disabled_metadata_keeps_original_instances() -> None:
    app = application(None)
    with TestClient(app) as client:
        assert client.get(f"/items/{uuid4()}").status_code == 200
    assert isinstance(app.state.closed[0], MemoryRepository)


def test_instrumentation_is_immutable_and_copied_by_feature() -> None:
    expected: list[type[Exception]] = [Missing]
    spec = instrument(Repository, expected=expected)
    declarations = [spec]
    feature = Feature(name="items", instrumentation=declarations)
    expected.clear()
    declarations.clear()

    assert spec.expected == (Missing,)
    assert feature.instrumentation == (spec,)
    with pytest.raises(FrozenInstanceError):
        spec.component = "other"  # type: ignore[misc]
    assert not hasattr(telemetry_api, "observe")


def test_feature_reuse_keeps_providers_and_app_telemetry_isolated() -> None:
    original = MemoryRepository()
    provider = Provider()
    provider.provide(lambda: original, provides=Repository, scope=Scope.APP)
    feature = Feature(
        name="items", providers=[provider], instrumentation=[instrument(Repository)]
    )

    with SpanCapture() as first, SpanCapture() as second:
        first_app = Composition(
            feature, telemetry=Telemetry(tracer_provider=first.provider)
        ).apply(FastAPI())
        second_app = Composition(
            feature, telemetry=Telemetry(tracer_provider=second.provider)
        ).apply(FastAPI())
        disabled = Composition(feature).apply(FastAPI())

        async def resolve() -> None:
            direct = make_async_container(provider)
            containers = (
                first_app.state.dishka_container,
                second_app.state.dishka_container,
                disabled.state.dishka_container,
                direct,
            )
            try:
                first_repository = await containers[0].get(Repository)
                second_repository = await containers[1].get(Repository)
                assert first_repository is not second_repository
                key = uuid4()
                assert await first_repository.get(key) == str(key)
                assert len(first.spans) == 1 and not second.spans
                assert await second_repository.get(key) == str(key)
                assert await containers[2].get(Repository) is original
                assert await direct.get(Repository) is original
                assert not provider.decorators
            finally:
                for container in containers:
                    await container.close()

        asyncio.run(asyncio.wait_for(resolve(), timeout=5))
        first_span = first.one("Repository.get")
        second_span = second.one("Repository.get")
        assert first_span.attributes is not None
        assert second_span.attributes is not None
        assert first_span.attributes["canon.feature"] == "items"
        assert second_span.attributes["canon.feature"] == "items"
        assert len(first.spans) == len(second.spans) == 1


def test_inherited_async_call_preserves_result_and_parentage() -> None:
    with SpanCapture() as capture:
        app = callable_application(Telemetry(tracer_provider=capture.provider))
        key = uuid4()
        with TestClient(app) as client:
            response = client.get(f"/callable/{key}")
        assert response.status_code == 200 and response.json() == str(key)
        capture.assert_tree(
            "GET /callable/{key}",
            [("fastapi.endpoint", [("CallableService.__call__", ["Repository.get"])])],
        )
        span = capture.one("CallableService.__call__")
        assert span.attributes is not None
        assert span.attributes["canon.feature"] == "items"
        assert span.attributes["code.function.name"] == "CallableService.__call__"


@pytest.mark.parametrize(
    ("fail", "status", "event", "span_status"),
    [
        ("expected", 404, "canon.port.expected_exception", StatusCode.UNSET),
        ("unexpected", 500, "exception", StatusCode.ERROR),
    ],
)
def test_async_call_keeps_exception_classification_and_privacy(
    fail: str, status: int, event: str, span_status: StatusCode
) -> None:
    with SpanCapture() as capture:
        app = callable_application(Telemetry(tracer_provider=capture.provider))
        with TestClient(app, raise_server_exceptions=False) as client:
            assert client.get(f"/callable/{uuid4()}?fail={fail}").status_code == status
        span = capture.one("CallableService.__call__")
        assert span.status.status_code is span_status
        assert len(span.events) == 1 and span.events[0].name == event
        assert span.events[0].attributes == {
            "exception.type": "Missing" if fail == "expected" else "ValueError"
        }
        assert "private" not in str(span.attributes) + str(span.events)
        assert span.attributes is not None
        assert ("error.type" in span.attributes) == (fail == "unexpected")


def test_async_call_cancellation_propagates_without_error() -> None:
    with SpanCapture() as capture:
        app = callable_application(Telemetry(tracer_provider=capture.provider))

        async def cancel() -> None:
            async with app.state.dishka_container() as request:
                service = await request.get(CallableService)
                with pytest.raises(asyncio.CancelledError):
                    await service(uuid4(), fail="cancel")

        with TestClient(app) as client:
            assert client.portal is not None
            client.portal.call(asyncio.wait_for, cancel(), 5)
        span = capture.one("CallableService.__call__")
        assert span.status.status_code is StatusCode.UNSET and not span.events


@pytest.mark.parametrize("exclude", [False, True])
def test_async_call_metrics_only_and_request_exclusion(exclude: bool) -> None:
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader])
    try:
        with SpanCapture() as capture:
            app = callable_application(
                Telemetry(
                    tracer_provider=capture.provider,
                    meter_provider=meter,
                    tracing=False,
                    metrics=False,
                    logs=False,
                    port_metrics=True,
                    exclude=lambda scope: exclude,
                )
            )
            with TestClient(app) as client:
                assert client.get(f"/callable/{uuid4()}").status_code == 200
            assert not capture.spans
            data = reader.get_metrics_data()
            if exclude:
                assert data is None
            else:
                assert data is not None
                histogram = next(
                    metric
                    for resource in data.resource_metrics
                    for scope in resource.scope_metrics
                    for metric in scope.metrics
                    if metric.name == "canon.port.duration"
                )
                assert isinstance(histogram.data, Histogram)
                assert {
                    point.attributes["code.function.name"]: point.count
                    for point in histogram.data.data_points
                    if point.attributes is not None
                } == {"CallableService.__call__": 1, "Repository.get": 1}
    finally:
        meter.shutdown()


def test_composition_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastAPI()
    monkeypatch.delattr(app, "_telemetry")
    with pytest.raises(FeatureConfigurationError, match=r"FastAPI >=0\.143"):
        Composition(telemetry=Telemetry()).apply(app)
    app = FastAPI()
    first = Telemetry()
    Composition(telemetry=first).apply(app)
    with pytest.raises(FeatureConfigurationError, match="different composition"):
        Composition(telemetry=Telemetry()).apply(app)
    started = FastAPI()
    with (
        TestClient(started),
        pytest.raises(FeatureConfigurationError, match="before the app starts"),
    ):
        Composition(telemetry=first).apply(started)


def test_invalid_instrumentation() -> None:
    class SyncPort:
        def run(self) -> str:
            return "sync"

    with pytest.raises(FeatureConfigurationError, match="public async methods"):
        instrument(SyncPort)
    with pytest.raises(FeatureConfigurationError, match="port must be a class"):
        instrument(object())  # type: ignore[arg-type]
    with pytest.raises(FeatureConfigurationError, match="Exception subclasses"):
        instrument(Repository, expected=[str])  # type: ignore[list-item]
    with pytest.raises(FeatureConfigurationError, match="component must be a string"):
        instrument(Repository, component=None)  # type: ignore[arg-type]
    with pytest.raises(
        FeatureConfigurationError, match=r"instrumentation.*Instrumentation"
    ):
        Feature(name="invalid", instrumentation=[Repository])  # type: ignore[list-item]


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("across_features", [False, True])
def test_duplicate_instrumentation_is_rejected(
    enabled: bool, across_features: bool
) -> None:
    features = (
        (
            Feature(name="first", instrumentation=[instrument(Repository)]),
            Feature(name="second", instrumentation=[instrument(Repository)]),
        )
        if across_features
        else (
            Feature(
                name="duplicate",
                instrumentation=[instrument(Repository), instrument(Repository)],
            ),
        )
    )
    app = FastAPI()
    with pytest.raises(FeatureConfigurationError, match="instrumented more than once"):
        Composition(*features, telemetry=Telemetry() if enabled else None).apply(app)
    assert not app.user_middleware


def test_rpc_forwards_providers() -> None:
    from fastapi_canon.integrations.websockets import (
        RpcChannel,
        RpcService,
        create_router,
    )

    with SpanCapture() as capture:
        options = Telemetry(tracer_provider=capture.provider)
        channel = RpcChannel("tasks")

        @channel.method("ping")
        async def ping() -> str:
            return "pong"

        service = RpcService(middleware=(options.rpc_middleware(),))
        service.socket("/rpc", channels=(channel,))
        app = Composition(
            Feature(name="rpc", routers=[create_router(service)]),
            telemetry=options,
        ).apply(FastAPI())
        with TestClient(app) as client, client.websocket_connect("/rpc") as websocket:
            websocket.send_json({"jsonrpc": "2.0", "id": 1, "method": "tasks.ping"})
            assert websocket.receive_json()["result"] == "pong"
        rpc = capture.one("tasks.ping")
        connection = capture.one("WS /rpc")
        endpoint = capture.one("fastapi.endpoint")
        assert rpc.parent is not None and endpoint.context is not None
        assert endpoint.parent is not None and connection.context is not None
        assert rpc.parent.span_id == endpoint.context.span_id
        assert endpoint.parent.span_id == connection.context.span_id
        assert (
            len([span for span in capture.spans if span.kind is SpanKind.SERVER]) == 2
        )


def test_capture_rejects_wrong_parentage() -> None:
    with SpanCapture() as capture:
        tracer = capture.provider.get_tracer("test")
        with (
            tracer.start_as_current_span("root"),
            tracer.start_as_current_span("child"),
            tracer.start_as_current_span("grandchild"),
        ):
            pass
        with pytest.raises(AssertionError, match="missing child"):
            capture.assert_tree("root", ["grandchild"])


def test_provider_scopes_caching_and_original_cleanup() -> None:
    closed: list[Repository] = []

    class Dependencies(Provider):
        @provide(scope=Scope.APP)
        async def repository(self) -> AsyncIterator[Repository]:
            original = MemoryRepository()
            yield original
            closed.append(original)

    with SpanCapture() as capture:
        app = Composition(
            Feature(
                name="app",
                providers=[Dependencies],
                instrumentation=[instrument(Repository)],
            ),
            telemetry=Telemetry(tracer_provider=capture.provider),
        ).apply(FastAPI())

        async def resolve() -> None:
            container = app.state.dishka_container
            first = await container.get(Repository)
            assert await container.get(Repository) is first
            await first.get(uuid4())
            assert type(first) is not MemoryRepository

        with TestClient(app) as client:
            assert client.portal is not None
            client.portal.call(resolve)
            assert not closed
        assert len(closed) == 1 and isinstance(closed[0], MemoryRepository)
        assert capture.one("Repository.get").attributes is not None


def test_request_exclusion_suppresses_port_spans_and_metrics() -> None:
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader])
    with SpanCapture() as capture:
        app = application(
            Telemetry(
                tracer_provider=capture.provider,
                meter_provider=meter,
                port_metrics=True,
                exclude=lambda scope: True,
            )
        )
        with TestClient(app) as client:
            assert client.get(f"/items/{uuid4()}").status_code == 200
        assert not capture.spans
        data = reader.get_metrics_data()
        assert data is None or not any(
            metric.data.data_points
            for resource in data.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
        )
    meter.shutdown()


def test_cancellation_propagates_without_error() -> None:
    with SpanCapture() as capture:
        app = application(Telemetry(tracer_provider=capture.provider))

        async def cancel() -> None:
            async with app.state.dishka_container() as request:
                repository = await request.get(Repository)
                with pytest.raises(asyncio.CancelledError):
                    await repository.get(uuid4(), fail="cancel")

        with TestClient(app) as client:
            assert client.portal is not None
            client.portal.call(cancel)
        span = capture.one("Repository.get")
        assert span.status.status_code is StatusCode.UNSET
        assert not span.events


def test_opt_out_of_problem_enrichment_and_port_spans() -> None:
    with SpanCapture() as capture:
        app = application(
            Telemetry(
                tracer_provider=capture.provider,
                problem_details=False,
                port_spans=False,
            )
        )
        with TestClient(app) as client:
            assert client.get(f"/items/{uuid4()}?fail=expected").status_code == 404
        assert not any(
            span.name in {"Service.run", "Repository.get"} for span in capture.spans
        )
        server = capture.one("GET /items/{key}")
        assert (
            server.attributes is not None
            and "canon.problem.code" not in server.attributes
        )


def test_globals_set_in_lifespan_are_resolved_lazily() -> None:
    script = textwrap.dedent("""
        from contextlib import asynccontextmanager
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from fastapi_canon import Composition, Feature
        from fastapi_canon.integrations.opentelemetry import Telemetry
        from fastapi_canon.testing.telemetry import SpanCapture
        from opentelemetry import trace

        with SpanCapture() as capture:
            @asynccontextmanager
            async def lifespan(app):
                trace.set_tracer_provider(capture.provider)
                yield
            app = FastAPI(lifespan=lifespan)
            @app.get("/health")
            async def health():
                return {"status": "ok"}
            Composition(telemetry=Telemetry()).apply(app)
            with TestClient(app) as client:
                assert client.get("/health").status_code == 200
            capture.one("GET /health")
            # Canon must not shut down the application's explicit/global provider.
            with capture.provider.get_tracer("after").start_as_current_span("after"):
                pass
            capture.one("after")
    """)
    result = subprocess.run(
        [sys.executable, "-I", "-c", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_native_auto_export_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FASTAPI_OTEL_AUTO_CONFIGURE", "true")
    app = Composition(telemetry=Telemetry()).apply(FastAPI())
    assert app._telemetry["auto_configure"] is False


def test_named_components_keep_feature_ownership() -> None:
    class Dependencies(Provider):
        repository = provide(MemoryRepository, provides=Repository, scope=Scope.APP)

    with SpanCapture() as capture:
        app = Composition(
            Feature(
                name="default",
                providers=[Dependencies],
                instrumentation=[instrument(Repository)],
            ),
            Feature(
                name="named",
                providers=[lambda: Dependencies(component="named")],
                instrumentation=[instrument(Repository, component="named")],
            ),
            telemetry=Telemetry(tracer_provider=capture.provider),
        ).apply(FastAPI())

        async def resolve() -> None:
            container = app.state.dishka_container
            await (await container.get(Repository)).get(uuid4())
            await (await container.get(Repository, component="named")).get(uuid4())

        with TestClient(app) as client:
            assert client.portal is not None
            client.portal.call(resolve)
        assert [
            span.attributes["canon.feature"]
            for span in capture.spans
            if span.attributes is not None
        ] == ["default", "named"]


def test_declarations_and_disabled_composition_import_no_otel_runtime() -> None:
    script = textwrap.dedent("""
        from dishka import Provider, Scope, provide
        from fastapi import FastAPI
        from fastapi_canon import Composition, Feature
        import sys
        from importlib.abc import MetaPathFinder
        class BlockOtel(MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.startswith("opentelemetry"):
                    raise ModuleNotFoundError("OTEL deliberately unavailable", name=fullname)
        sys.meta_path.insert(0, BlockOtel())
        for name in tuple(sys.modules):
            if name.startswith("opentelemetry"):
                del sys.modules[name]
        from fastapi_canon.integrations.opentelemetry import Telemetry, instrument
        class Port:
            async def run(self): pass
        class Dependencies(Provider):
            port = provide(Port, scope=Scope.REQUEST)
        Composition(Feature(
            name="test", providers=[Dependencies], instrumentation=[instrument(Port)]
        )).apply(FastAPI())
        assert not any(name.startswith("opentelemetry") for name in sys.modules)
        try:
            Composition(telemetry=Telemetry()).apply(FastAPI())
        except ModuleNotFoundError as error:
            assert "fastapi-canon[otel]" in str(error)
        else:
            raise AssertionError("enabling telemetry must require the extra")
    """)
    result = subprocess.run(
        [sys.executable, "-I", "-c", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
