from copy import deepcopy
from dataclasses import FrozenInstanceError
from typing import Any

import pytest
from dishka import Provider, Scope, provide
from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Depends, FastAPI, Header
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from fastapi_canon import (
    CanonResponse,
    CanonRouter,
    Composition,
    Error,
    ErrorConfigurationError,
    ErrorRegistry,
    Feature,
    FeatureConfigurationError,
    RouterPolicy,
)


class AuthenticationRequired(Exception):
    pass


class Forbidden(Exception):
    pass


class MissingItem(Exception):
    pass


AUTHENTICATION_REQUIRED = Error(
    AuthenticationRequired,
    status=401,
    code="authentication_required",
    title="Authentication required",
    headers={"WWW-Authenticate": "Bearer"},
    openapi_headers={"WWW-Authenticate": {"schema": {"type": "string"}}},
)
FORBIDDEN = Error(Forbidden, status=403, code="forbidden", title="Forbidden")
MISSING = Error(MissingItem, status=403, code="missing_item", title="Missing item")
AUTH_ERRORS = ErrorRegistry(
    errors=[AUTHENTICATION_REQUIRED, FORBIDDEN],
    type_base="https://example.test/problems",
)
ITEM_ERRORS = ErrorRegistry(errors=[MISSING], type_base="https://example.test/problems")


async def require_user(authorization: str | None = Header(default=None)) -> None:
    if authorization != "Bearer test":
        raise AuthenticationRequired


PROTECTED = RouterPolicy(
    error_registry=AUTH_ERRORS,
    dependencies=[Depends(require_user)],
    raises=[AUTHENTICATION_REQUIRED, FORBIDDEN],
)


def test_policy_copies_sequences_and_is_frozen() -> None:
    dependencies = [Depends(require_user)]
    errors = [AUTHENTICATION_REQUIRED]
    policy = RouterPolicy(
        error_registry=AUTH_ERRORS, dependencies=dependencies, raises=errors
    )
    dependencies.clear()
    errors.clear()
    assert len(policy.dependencies) == 1
    assert policy.raises == (AUTHENTICATION_REQUIRED,)
    with pytest.raises(FrozenInstanceError):
        policy.raises = ()  # type: ignore[misc]


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"error_registry": object()}, "error_registry must be"),
        ({"raises": [AUTHENTICATION_REQUIRED]}, "requires an error registry"),
        ({"raises": "invalid"}, "must be a sequence"),
        ({"dependencies": None}, "must be a sequence"),
        ({"dependencies": "invalid"}, "must be a sequence"),
        ({"dependencies": [object()]}, "Depends instance"),
        ({"error_registry": AUTH_ERRORS, "raises": [MISSING]}, "does not belong"),
    ],
)
def test_policy_rejects_invalid_configuration(
    options: dict[str, Any], message: str
) -> None:
    with pytest.raises(ErrorConfigurationError, match=message):
        RouterPolicy(**options)


def test_router_policy_combines_contracts_dependencies_and_dishka_injection() -> None:
    calls: list[str] = []

    async def local_dependency() -> None:
        calls.append("local")

    class Services(Provider):
        @provide(scope=Scope.REQUEST)
        def value(self) -> str:
            return "injected"

    router = CanonRouter(
        policy=PROTECTED,
        error_registry=ITEM_ERRORS,
        raises=[FORBIDDEN],
        dependencies=[Depends(local_dependency)],
        route_class=DishkaRoute,
    )

    @router.get("/items", raises=[MISSING], response=CanonResponse.json())
    async def items(value: FromDishka[str]) -> str:
        return value

    feature = Feature(name="items", routers=[router], providers=[Services()])
    composition = Composition(feature)
    app = composition.apply(FastAPI())
    composition.validate(app)
    with TestClient(app) as client:
        response = client.get("/items")
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert response.json()["code"] == "authentication_required"
        assert calls == []
        assert (
            client.get("/items", headers={"Authorization": "Bearer test"}).json()
            == "injected"
        )
    assert calls == ["local"]
    responses = app.openapi()["paths"]["/items"]["get"]["responses"]
    assert set(responses) >= {"200", "401", "403"}
    assert set(
        responses["403"]["content"]["application/problem+json"]["schema"][
            "discriminator"
        ]["mapping"]
    ) == {"forbidden", "missing_item"}
    assert router.policy is PROTECTED
    assert router.raises == (AUTHENTICATION_REQUIRED, FORBIDDEN)
    assert feature.errors is not None and feature.errors.contains(MISSING)


def test_composition_policy_preserves_nested_router_definitions() -> None:
    child = CanonRouter(error_registry=ITEM_ERRORS)

    @child.get("/items/{item_id}", raises=[MISSING])
    async def item(item_id: int) -> int:
        if item_id == 0:
            raise MissingItem
        return item_id

    @child.get("/hidden", include_in_schema=False)
    async def hidden() -> None:
        return None

    route = child.routes[0]
    assert isinstance(route, APIRoute)
    original_responses = deepcopy(route.responses)
    parent = APIRouter()
    parent.include_router(child)
    # An explicit registry is needed for a generic parent, as before.
    feature = Feature(name="items", routers=[parent], errors=ITEM_ERRORS)
    composition = Composition(
        feature,
        router_policy=PROTECTED,
        router_factory=lambda: CanonRouter(prefix="/api"),
    )
    protected = FastAPI()
    protected.get("/health")(lambda: "ok")
    composition.apply(protected)
    protected.get("/later")(lambda: "later")
    composition.validate(protected)
    public = Composition(feature).apply(FastAPI())

    with TestClient(protected) as client:
        assert client.get("/api/items/1").status_code == 401
        assert client.get("/api/hidden").status_code == 401
        assert client.get("/health").status_code == 200
        assert client.get("/later").status_code == 200
        assert (
            client.get("/api/items/0", headers={"Authorization": "Bearer test"}).json()[
                "code"
            ]
            == "missing_item"
        )
    assert TestClient(public).get("/items/1").status_code == 200
    protected_schema = protected.openapi()
    responses = protected_schema["paths"]["/api/items/{item_id}"]["get"]["responses"]
    assert "401" in responses
    assert (
        len(responses["403"]["content"]["application/problem+json"]["schema"]["oneOf"])
        == 2
    )
    assert "WWW-Authenticate" in responses["401"]["headers"]
    assert "401" not in protected_schema["paths"]["/health"]["get"]["responses"]
    assert "401" not in protected_schema["paths"]["/later"]["get"]["responses"]
    assert (
        "401" not in public.openapi()["paths"]["/items/{item_id}"]["get"]["responses"]
    )
    assert "x-fastapi-canon" not in str(protected_schema)
    assert route.responses == original_responses
    assert child.dependencies == []
    assert child.raises == ()


def test_composition_policy_identity_is_part_of_installation() -> None:
    feature = Feature(name="empty")
    composition = Composition(feature, router_policy=PROTECTED)
    app = composition.apply(FastAPI())
    assert composition.apply(app) is app
    Composition(feature, router_policy=PROTECTED).validate(app)
    different = Composition(feature, router_policy=RouterPolicy())
    with pytest.raises(FeatureConfigurationError, match="different composition"):
        different.apply(app)
    with pytest.raises(FeatureConfigurationError, match="before validation"):
        different.validate(app)


@pytest.mark.parametrize("scope", ["router", "composition"])
def test_invalid_policy_type_is_rejected(scope: str) -> None:
    with pytest.raises(ErrorConfigurationError, match="RouterPolicy instance"):
        if scope == "router":
            CanonRouter(policy=object())  # type: ignore[arg-type]
        else:
            Composition(router_policy=object())  # type: ignore[arg-type]


@pytest.mark.parametrize("scope", ["router", "composition"])
def test_policy_registry_collisions_are_rejected(scope: str) -> None:
    other = Error(Forbidden, status=403, code="other_forbidden", title="Other")
    registry = ErrorRegistry(errors=[other], type_base=AUTH_ERRORS.type_base)
    app = FastAPI()
    original_routes = tuple(app.routes)
    with pytest.raises(ErrorConfigurationError, match="exception"):
        if scope == "router":
            CanonRouter(policy=PROTECTED, error_registry=registry)
        else:
            Composition(
                Feature(name="other", errors=registry), router_policy=PROTECTED
            ).apply(app)
    assert tuple(app.routes) == original_routes


def test_composition_policy_rejects_manual_collision_before_installation() -> None:
    router = CanonRouter()
    router.get("/items", responses={401: {"description": "Manual error"}})(lambda: None)
    app = FastAPI()
    original_routes = tuple(app.routes)
    with pytest.raises(FeatureConfigurationError, match="policy response status 401"):
        Composition(
            Feature(name="items", routers=[router]), router_policy=PROTECTED
        ).apply(app)
    assert tuple(app.routes) == original_routes
    assert app.openapi_schema is None


def test_dependency_only_policy_needs_no_registry_and_deduplicates_execution() -> None:
    calls: list[str] = []

    async def prerequisite() -> None:
        calls.append("called")

    policy = RouterPolicy(dependencies=[Depends(prerequisite)])
    router = CanonRouter(policy=policy)
    router.get("/items")(lambda: None)
    app = Composition(
        Feature(name="items", routers=[router]), router_policy=policy
    ).apply(FastAPI())
    assert TestClient(app).get("/items").status_code == 200
    assert calls == ["called"]
    assert "Problem" not in app.openapi()["components"]["schemas"]


def test_existing_parent_raises_remain_local_to_parent_operations() -> None:
    parent = CanonRouter(error_registry=AUTH_ERRORS, raises=[FORBIDDEN])
    child = CanonRouter()
    child.get("/child")(lambda: None)
    parent.include_router(child)
    app = Composition(Feature(name="parent", routers=[parent])).apply(FastAPI())
    assert "403" not in app.openapi()["paths"]["/child"]["get"]["responses"]


def test_composition_policy_422_replaces_automatic_validation() -> None:
    invalid = Error(ValueError, status=422, code="invalid_input", title="Invalid input")
    policy = RouterPolicy(
        error_registry=ErrorRegistry(errors=[invalid], type_base=AUTH_ERRORS.type_base),
        raises=[invalid],
    )
    router = CanonRouter()

    @router.get("/items/{item_id}")
    async def item(item_id: int) -> int:
        return item_id

    app = Composition(
        Feature(name="items", routers=[router]), router_policy=policy
    ).apply(FastAPI())
    response = app.openapi()["paths"]["/items/{item_id}"]["get"]["responses"]["422"]
    assert response["content"]["application/problem+json"]["schema"] == {
        "$ref": "#/components/schemas/InvalidInputProblem"
    }
