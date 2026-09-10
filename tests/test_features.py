from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError

import pytest
from dishka import FromDishka, Provider, Scope, provide
from dishka.integrations.fastapi import inject
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.responses import Response

from fastapi_canon import (
    CanonResponse,
    CanonRouter,
    Composition,
    ErrorOptions,
    ExceptionHandlerSpec,
    Feature,
    FeatureConfigurationError,
)
from fastapi_canon.error import Error, ErrorRegistry


def test_feature_copies_inputs_and_is_frozen() -> None:
    router = APIRouter()
    routers = [router]

    feature = Feature(name="catalog", routers=routers)
    routers.clear()

    assert feature.routers == (router,)
    with pytest.raises(FrozenInstanceError):
        feature.routers = ()  # type: ignore[misc]


@pytest.mark.parametrize("name", ["", "   ", "two\nlines"])
def test_feature_requires_a_diagnostic_name(name: str) -> None:
    with pytest.raises(FeatureConfigurationError, match="feature name"):
        Feature(name=name)


def test_composition_rejects_duplicate_feature_names() -> None:
    with pytest.raises(FeatureConfigurationError, match=r"catalog.*duplicated"):
        Composition(Feature(name="catalog"), Feature(name="catalog"))


def test_composition_includes_routers_in_declaration_order() -> None:
    first = APIRouter()
    second = APIRouter()

    @first.get("/same")
    async def first_route() -> dict[str, str]:
        return {"feature": "first"}

    @second.get("/same")
    async def second_route() -> dict[str, str]:
        return {"feature": "second"}

    app = Composition(
        Feature(name="first", routers=[first]),
        Feature(name="second", routers=[second]),
    ).apply(FastAPI())

    assert TestClient(app).get("/same").json() == {"feature": "first"}


def test_composition_router_factory_applies_shared_router_configuration() -> None:
    calls: list[str] = []
    created: list[APIRouter] = []

    class ApplicationRouter(APIRouter):
        pass

    async def shared_dependency() -> None:
        calls.append("dependency")

    def router_factory() -> APIRouter:
        router = ApplicationRouter(
            prefix="/api/v1",
            tags=["application"],
            dependencies=[Depends(shared_dependency)],
        )
        created.append(router)
        return router

    catalog = APIRouter(prefix="/catalog")

    @catalog.get("/items")
    async def list_items() -> list[str]:
        return []

    app = Composition(
        Feature(name="catalog", routers=[catalog]),
        router_factory=router_factory,
    ).apply(FastAPI())

    response = TestClient(app).get("/api/v1/catalog/items")

    assert response.status_code == 200
    assert calls == ["dependency"]
    assert len(created) == 1
    assert isinstance(created[0], ApplicationRouter)
    assert app.openapi()["paths"]["/api/v1/catalog/items"]["get"]["tags"] == [
        "application"
    ]


def test_router_factory_must_return_a_fresh_router() -> None:
    router = APIRouter()
    router.get("/existing")(lambda: None)
    app = FastAPI()
    original_routes = tuple(app.routes)

    with pytest.raises(FeatureConfigurationError, match="fresh APIRouter"):
        Composition(
            Feature(name="catalog"),
            router_factory=lambda: router,
        ).apply(app)

    assert tuple(app.routes) == original_routes


def test_composition_is_idempotent_for_same_declarations() -> None:
    router = APIRouter()
    feature = Feature(name="catalog", routers=[router])
    composition = Composition(feature)
    app = FastAPI()

    first = composition.apply(app)
    route_count = len(app.routes)
    second = composition.apply(app)

    assert first is app
    assert second is app
    assert len(app.routes) == route_count


def test_composition_rejects_a_different_second_composition() -> None:
    app = FastAPI()
    Composition(Feature(name="first")).apply(app)

    with pytest.raises(FeatureConfigurationError, match="different composition"):
        Composition(Feature(name="second")).apply(app)


def test_composition_and_error_options_are_immutable() -> None:
    feature = Feature(name="catalog")
    options = ErrorOptions(type_base="https://example.test/problems")
    composition = Composition(feature, errors=options)

    assert composition.features == (feature,)
    assert composition.errors is options
    with pytest.raises(FrozenInstanceError):
        composition.features = ()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        options.type_base = None  # type: ignore[misc]


def test_error_options_reject_invalid_values() -> None:
    with pytest.raises(FeatureConfigurationError, match="type_base"):
        ErrorOptions(type_base=42)  # type: ignore[arg-type]

    with pytest.raises(FeatureConfigurationError, match="include_http_exceptions"):
        ErrorOptions(include_http_exceptions=1)  # type: ignore[arg-type]


def test_composition_rejects_shared_router_without_partial_application() -> None:
    router = APIRouter()
    app = FastAPI()
    original_routes = tuple(app.routes)

    with pytest.raises(FeatureConfigurationError, match="same router"):
        Composition(
            Feature(name="catalog", routers=[router]),
            Feature(name="orders", routers=[router]),
        ).apply(app)

    assert tuple(app.routes) == original_routes


class MissingDependency:
    pass


class InvalidProvider(Provider):
    @provide(scope=Scope.APP)
    def value(self, dependency: MissingDependency) -> str:
        return str(dependency)


def test_composition_validates_dishka_graph_without_partial_application() -> None:
    router = APIRouter()
    app = FastAPI()
    original_routes = tuple(app.routes)

    with pytest.raises(FeatureConfigurationError, match="invalid Dishka provider"):
        Composition(
            Feature(name="catalog", routers=[router], providers=[InvalidProvider()]),
        ).apply(app)

    assert tuple(app.routes) == original_routes


class ResourceProvider(Provider):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    @provide(scope=Scope.APP)
    async def resource(self) -> AsyncIterator[str]:
        self.events.append("provider start")
        yield "ready"
        self.events.append("provider stop")


def test_provider_class_and_factory_are_materialized_during_apply() -> None:
    created: list[str] = []

    class ClassProvider(Provider):
        def __init__(self) -> None:
            super().__init__()
            created.append("class")

    def provider_factory() -> Provider:
        created.append("factory")
        return Provider()

    feature = Feature(
        name="providers",
        providers=[ClassProvider, provider_factory],
    )

    assert created == []

    Composition(feature).apply(FastAPI())

    assert created == ["class", "factory"]


def test_invalid_provider_factory_result_names_its_feature() -> None:
    def invalid_factory() -> Provider:
        return "not a provider"  # type: ignore[return-value]

    with pytest.raises(FeatureConfigurationError, match=r"feature 'catalog'.*str"):
        Composition(
            Feature(name="catalog", providers=[invalid_factory]),
        ).apply(FastAPI())


def test_composition_builds_and_closes_shared_container() -> None:
    events: list[str] = []
    router = APIRouter()

    @router.get("/resource")
    @inject
    async def resource(value: FromDishka[str]) -> dict[str, str]:
        return {"value": value}

    app = Composition(
        Feature(
            name="resources",
            routers=[router],
            providers=[ResourceProvider(events)],
        ),
    ).apply(FastAPI())

    container = app.state.dishka_container
    with TestClient(app) as client:
        assert client.get("/resource").json() == {"value": "ready"}

    assert events == ["provider start", "provider stop"]
    assert app.state.dishka_container is container


def test_lifespans_start_in_order_and_stop_in_reverse_order() -> None:
    events: list[str] = []

    @asynccontextmanager
    async def app_lifespan(app: FastAPI) -> AsyncIterator[None]:
        del app
        events.append("app start")
        yield
        events.append("app stop")

    def lifespan(name: str):  # type: ignore[no-untyped-def]
        @asynccontextmanager
        async def manager(app: FastAPI) -> AsyncIterator[None]:
            del app
            events.append(f"{name} start")
            yield
            events.append(f"{name} stop")

        return manager

    app = FastAPI(lifespan=app_lifespan)
    Composition(
        Feature(name="first", lifespan=lifespan("first")),
        Feature(name="second", lifespan=lifespan("second")),
    ).apply(app)

    with TestClient(app):
        assert events == ["app start", "first start", "second start"]

    assert events == [
        "app start",
        "first start",
        "second start",
        "second stop",
        "first stop",
        "app stop",
    ]


def test_lifespan_cleans_up_when_later_startup_fails() -> None:
    events: list[str] = []

    @asynccontextmanager
    async def started(app: FastAPI) -> AsyncIterator[None]:
        del app
        events.append("start")
        try:
            yield
        finally:
            events.append("stop")

    @asynccontextmanager
    async def failing(app: FastAPI) -> AsyncIterator[None]:
        if app.state.fail_startup:
            raise RuntimeError("startup failed")
        yield

    app = FastAPI()
    app.state.fail_startup = True
    Composition(
        Feature(name="started", lifespan=started),
        Feature(name="failing", lifespan=failing),
    ).apply(app)

    with pytest.raises(RuntimeError, match="startup failed"), TestClient(app):
        pass

    assert events == ["start", "stop"]


class NotFoundError(Exception):
    pass


def test_error_registries_are_merged_for_runtime_and_openapi() -> None:
    error = Error(
        NotFoundError,
        status=404,
        code="item_not_found",
        title="Item not found",
    )
    errors = ErrorRegistry(name="items", errors=[error])
    router = APIRouter()

    @router.get("/items/{item_id}", responses=errors.responses(error))
    async def get_item(item_id: str) -> None:
        del item_id
        raise NotFoundError

    app = Composition(
        Feature(name="items", routers=[router], errors=errors),
        errors=ErrorOptions(type_base="https://example.test/problems"),
    ).apply(FastAPI())

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/items/missing")

    assert response.status_code == 404
    assert response.json()["type"] == ("https://example.test/problems/item_not_found")
    assert (
        app.openapi()["paths"]["/items/{item_id}"]["get"]["responses"]["404"]
        is not None
    )


def test_feature_collects_error_registry_from_canon_router() -> None:
    error = Error(
        NotFoundError,
        status=404,
        code="item_not_found",
        title="Item not found",
    )
    errors = ErrorRegistry(name="items", errors=[error])
    router = CanonRouter(error_registry=errors)

    @router.get("/items/{item_id}", raises=[error])
    async def get_item(item_id: str) -> None:
        del item_id
        raise NotFoundError

    feature = Feature(name="items", routers=[router])
    app = Composition(
        feature,
        errors=ErrorOptions(type_base="https://example.test/problems"),
    ).apply(FastAPI())

    assert feature.errors is not None
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/items/missing")
    assert response.status_code == 404
    assert response.json()["code"] == "item_not_found"


def test_shared_router_registry_is_contributed_only_once() -> None:
    error = Error(
        NotFoundError,
        status=404,
        code="item_not_found",
        title="Item not found",
    )
    errors = ErrorRegistry(name="items", errors=[error])
    first = CanonRouter(error_registry=errors)
    second = CanonRouter(error_registry=errors)
    first.get("/first", raises=[error])(lambda: None)
    second.get("/second", raises=[error])(lambda: None)

    app = Composition(
        Feature(name="first", routers=[first]),
        Feature(name="second", routers=[second]),
        errors=ErrorOptions(type_base="https://example.test/problems"),
    ).apply(FastAPI())

    assert "404" in app.openapi()["paths"]["/first"]["get"]["responses"]
    assert "404" in app.openapi()["paths"]["/second"]["get"]["responses"]


def test_composition_installs_success_openapi_without_an_error_registry() -> None:
    router = APIRouter()
    router.get(
        "/ready",
        status_code=307,
        responses=CanonResponse.empty(status=307).responses(),
    )(lambda: None)
    app = Composition(Feature(name="health", routers=[router])).apply(FastAPI())

    schema = app.openapi()

    assert schema["paths"]["/ready"]["get"]["responses"] == {
        "307": {"description": "No content"}
    }
    assert "x-fastapi-canon" not in str(schema)


def test_error_collision_does_not_install_routers() -> None:
    class OtherNotFoundError(Exception):
        pass

    first = Error(
        NotFoundError,
        status=404,
        code="same_code",
        title="First",
    )
    second = Error(
        OtherNotFoundError,
        status=404,
        code="same_code",
        title="Second",
    )
    router = APIRouter()
    app = FastAPI()
    original_routes = tuple(app.routes)

    with pytest.raises(FeatureConfigurationError, match="conflicting code"):
        Composition(
            Feature(
                name="first",
                routers=[router],
                errors=ErrorRegistry(name="first", errors=[first]),
            ),
            Feature(
                name="second",
                errors=ErrorRegistry(name="second", errors=[second]),
            ),
            errors=ErrorOptions(type_base="https://example.test/problems"),
        ).apply(app)

    assert tuple(app.routes) == original_routes


def test_feature_exception_handler_is_installed() -> None:
    class TeapotError(Exception):
        pass

    async def handler(request: Request, exception: Exception) -> Response:
        del request, exception
        return JSONResponse({"handled": True}, status_code=418)

    router = APIRouter()

    @router.get("/teapot")
    async def teapot() -> None:
        raise TeapotError

    app = Composition(
        Feature(
            name="teapot",
            routers=[router],
            exception_handlers=[ExceptionHandlerSpec(TeapotError, handler)],
        ),
    ).apply(FastAPI())

    with TestClient(app) as client:
        response = client.get("/teapot")

    assert response.status_code == 418
    assert response.json() == {"handled": True}
