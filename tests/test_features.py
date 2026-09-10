from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError

import pytest
from dishka import FromDishka, Provider, Scope, provide
from dishka.integrations.fastapi import inject
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.responses import Response

from fastapi_canon import (
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

    feature = Feature(routers=routers)
    routers.clear()

    assert feature.routers == (router,)
    with pytest.raises(FrozenInstanceError):
        feature.routers = ()  # type: ignore[misc]


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
        Feature(routers=[first]),
        Feature(routers=[second]),
    ).apply(FastAPI())

    assert TestClient(app).get("/same").json() == {"feature": "first"}


def test_composition_is_idempotent_for_same_declarations() -> None:
    router = APIRouter()
    feature = Feature(routers=[router])
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
    Composition(Feature()).apply(app)

    with pytest.raises(FeatureConfigurationError, match="different composition"):
        Composition(Feature()).apply(app)


def test_composition_and_error_options_are_immutable() -> None:
    feature = Feature()
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
            Feature(routers=[router]),
            Feature(routers=[router]),
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
            Feature(routers=[router], providers=[InvalidProvider()]),
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


def test_composition_builds_and_closes_shared_container() -> None:
    events: list[str] = []
    router = APIRouter()

    @router.get("/resource")
    @inject
    async def resource(value: FromDishka[str]) -> dict[str, str]:
        return {"value": value}

    app = Composition(
        Feature(routers=[router], providers=[ResourceProvider(events)]),
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
        Feature(lifespan=lifespan("first")),
        Feature(lifespan=lifespan("second")),
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
        Feature(lifespan=started),
        Feature(lifespan=failing),
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
        Feature(routers=[router], errors=errors),
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
                routers=[router],
                errors=ErrorRegistry(name="first", errors=[first]),
            ),
            Feature(errors=ErrorRegistry(name="second", errors=[second])),
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
            routers=[router],
            exception_handlers=[ExceptionHandlerSpec(TeapotError, handler)],
        ),
    ).apply(FastAPI())

    with TestClient(app) as client:
        response = client.get("/teapot")

    assert response.status_code == 418
    assert response.json() == {"handled": True}
