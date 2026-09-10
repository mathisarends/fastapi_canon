from typing import Any, cast

import pytest
from fastapi import APIRouter, FastAPI, Query
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from openapi_spec_validator import validate
from pydantic import BaseModel, ConfigDict

from fastapi_canon.error import Error, ErrorConfigurationError, ErrorRegistry


class Missing(Exception):
    pass


class Conflict(Exception):
    pass


class MissingFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_id: int


def make_error(
    exception: type[Exception] = Missing,
    *,
    status: int = 404,
    code: str = "resource_missing",
    title: str = "Resource missing",
    **kwargs: Any,
) -> Error[Exception]:
    return Error(
        exception,
        status=status,
        code=code,
        title=title,
        **kwargs,
    )


def installed_app(*errors: Error[Any]) -> tuple[FastAPI, ErrorRegistry]:
    registry = ErrorRegistry(
        errors=list(errors),
        type_base="https://example.test/problems",
    )
    return FastAPI(), registry


def test_single_error_generates_openapi_component_and_response() -> None:
    missing = make_error(description="The requested resource does not exist.")
    app, registry = installed_app(missing)
    router = APIRouter()

    @router.get("/resources/{resource_id}", responses=registry.responses(missing))
    async def endpoint(resource_id: int) -> None:
        return None

    app.include_router(router)
    registry.install(app)
    document = app.openapi()
    validate(document)

    schema = document["components"]["schemas"]["ResourceMissingProblem"]
    response = document["paths"]["/resources/{resource_id}"]["get"]["responses"]["404"]
    assert schema["properties"]["type"]["const"] == (
        "https://example.test/problems/resource_missing"
    )
    assert schema["properties"]["status"]["const"] == 404
    assert schema["properties"]["code"]["const"] == "resource_missing"
    assert schema["required"] == ["type", "title", "status", "code"]
    assert schema["additionalProperties"] is True
    assert response == {
        "description": "The requested resource does not exist.",
        "content": {
            "application/problem+json": {
                "schema": {"$ref": "#/components/schemas/ResourceMissingProblem"}
            }
        },
    }


def test_same_status_generates_discriminated_one_of() -> None:
    missing = make_error()
    hidden = make_error(
        Conflict,
        code="resource_hidden",
        title="Resource hidden",
    )
    app, registry = installed_app(missing, hidden)
    router = APIRouter()

    @router.get("/resource", responses=registry.responses(missing, hidden))
    async def endpoint() -> None:
        return None

    app.include_router(router)
    registry.install(app)
    response = app.openapi()["paths"]["/resource"]["get"]["responses"]["404"]
    schema = response["content"]["application/problem+json"]["schema"]

    assert schema["oneOf"] == [
        {"$ref": "#/components/schemas/ResourceMissingProblem"},
        {"$ref": "#/components/schemas/ResourceHiddenProblem"},
    ]
    assert schema["discriminator"] == {
        "propertyName": "code",
        "mapping": {
            "resource_missing": "#/components/schemas/ResourceMissingProblem",
            "resource_hidden": "#/components/schemas/ResourceHiddenProblem",
        },
    }


def test_typed_extensions_headers_and_example_are_documented() -> None:
    example = {
        "type": "https://example.test/problems/resource_missing",
        "title": "Resource missing",
        "status": 404,
        "code": "resource_missing",
        "resource_id": 7,
    }
    missing = make_error(
        extensions_model=MissingFields,
        extensions=lambda _error: MissingFields(resource_id=7),
        openapi_headers={
            "Retry-After": {
                "description": "Seconds before retrying.",
                "schema": {"type": "integer"},
            }
        },
        example=example,
    )
    app, registry = installed_app(missing)
    router = APIRouter()
    router.get("/resource", responses=registry.responses(missing))(lambda: None)
    app.include_router(router)
    registry.install(app)
    document = app.openapi()

    schema = document["components"]["schemas"]["ResourceMissingProblem"]
    response = document["paths"]["/resource"]["get"]["responses"]["404"]
    assert schema["properties"]["resource_id"]["type"] == "integer"
    assert "resource_id" in schema["required"]
    assert schema["examples"] == [example]
    assert response["headers"]["Retry-After"]["schema"] == {"type": "integer"}


def test_static_extensions_are_required_constants() -> None:
    missing = make_error(extensions={"retryable": False, "attempts": 2})
    app, registry = installed_app(missing)
    registry.install(app)
    schema = app.openapi()["components"]["schemas"]["ResourceMissingProblem"]

    assert schema["properties"]["retryable"] == {
        "const": False,
        "type": "boolean",
    }
    assert schema["properties"]["attempts"] == {"const": 2, "type": "integer"}


def test_runtime_payload_validates_against_documented_schema() -> None:
    missing = make_error(
        detail="Nothing is here.",
        headers={"Retry-After": "30"},
        openapi_headers={"Retry-After": {"schema": {"type": "integer"}}},
    )
    app, registry = installed_app(missing)
    router = APIRouter()

    @router.get("/resource", responses=registry.responses(missing))
    async def endpoint() -> None:
        raise Missing

    app.include_router(router)
    registry.install(app)
    document = app.openapi()
    response = TestClient(app).get("/resource")
    documented_response = document["paths"]["/resource"]["get"]["responses"]["404"]
    schema = document["components"]["schemas"]["ResourceMissingProblem"]

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["retry-after"] == "30"
    assert documented_response["headers"]["Retry-After"] == {
        "schema": {"type": "integer"}
    }
    Draft202012Validator(schema).validate(response.json())


def test_custom_openapi_wrapper_and_cache_are_preserved() -> None:
    missing = make_error()
    app, registry = installed_app(missing)
    router = APIRouter()
    router.get("/resource", responses=registry.responses(missing))(lambda: None)
    app.include_router(router)
    original = app.openapi
    calls = 0

    def custom_openapi() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        document = original()
        document["x-owner"] = "application"
        return document

    cast("Any", app).openapi = custom_openapi
    registry.install(app)

    first = app.openapi()
    second = app.openapi()
    assert first is second
    assert first["x-owner"] == "application"
    assert calls == 1


def test_request_validation_contract_replaces_fastapi_default() -> None:
    app, registry = installed_app()

    @app.get("/resource")
    async def endpoint(limit: int = Query(gt=0)) -> None:
        return None

    registry.install(app)
    document = app.openapi()
    response = document["paths"]["/resource"]["get"]["responses"]["422"]

    assert "application/json" not in response["content"]
    assert response["content"]["application/problem+json"]["schema"] == {
        "$ref": "#/components/schemas/RequestValidationProblem"
    }
    runtime = TestClient(app).get("/resource", params={"limit": "bad"})
    schema = document["components"]["schemas"]["RequestValidationProblem"]
    Draft202012Validator(schema).validate(runtime.json())


def test_registry_responses_supports_stock_api_router() -> None:
    missing = make_error()
    app, registry = installed_app(missing)
    router = APIRouter()
    router.get("/resource", responses=registry.responses(missing))(lambda: None)
    app.include_router(router)
    registry.install(app)
    response = app.openapi()["paths"]["/resource"]["get"]["responses"]["404"]

    assert response["content"]["application/problem+json"]["schema"] == {
        "$ref": "#/components/schemas/ResourceMissingProblem"
    }


def test_responses_rejects_foreign_and_allows_late_type_resolution() -> None:
    missing = make_error()
    foreign = make_error(Conflict, code="resource_conflict")
    resolved = ErrorRegistry(
        errors=[missing], type_base="https://example.test/problems"
    )
    unresolved = ErrorRegistry(errors=[missing])

    with pytest.raises(ErrorConfigurationError, match="does not belong"):
        resolved.responses(foreign)
    assert 404 in unresolved.responses(missing)


def test_existing_component_collision_fails_loudly() -> None:
    missing = make_error()
    app, registry = installed_app(missing)
    original = app.openapi

    def custom_openapi() -> dict[str, Any]:
        document = original()
        document.setdefault("components", {}).setdefault("schemas", {})[
            "ResourceMissingProblem"
        ] = {"type": "string"}
        return document

    cast("Any", app).openapi = custom_openapi
    registry.install(app)

    with pytest.raises(ErrorConfigurationError, match="already exists"):
        app.openapi()


def test_repeated_lazy_router_inclusion_compiles_each_effective_path() -> None:
    missing = make_error()
    app, registry = installed_app(missing)
    router = APIRouter()
    router.get("/resource", responses=registry.responses(missing))(lambda: None)
    app.include_router(router, prefix="/one")
    app.include_router(router, prefix="/two")
    registry.install(app)
    paths = app.openapi()["paths"]

    assert "404" in paths["/one/resource"]["get"]["responses"]
    assert "404" in paths["/two/resource"]["get"]["responses"]


@pytest.mark.parametrize(
    ("value", "json_type"),
    [
        (None, "null"),
        (1.5, "number"),
        ("fixed", "string"),
        ([1, 2], "array"),
        ({"enabled": True}, "object"),
    ],
)
def test_static_extension_schema_uses_exact_json_type(
    value: Any, json_type: str
) -> None:
    missing = make_error(extensions={"value": value})
    app, registry = installed_app(missing)
    registry.install(app)
    property_schema = app.openapi()["components"]["schemas"]["ResourceMissingProblem"][
        "properties"
    ]["value"]

    assert property_schema == {"const": value, "type": json_type}


def test_invalid_example_constant_fails_during_openapi_compilation() -> None:
    missing = make_error(
        example={
            "type": "https://example.test/problems/resource_missing",
            "title": "Wrong title",
            "status": 404,
            "code": "resource_missing",
        }
    )
    app, registry = installed_app(missing)
    registry.install(app)

    with pytest.raises(ErrorConfigurationError, match="invalid 'title'"):
        app.openapi()


def test_invalid_static_extension_example_fails_compilation() -> None:
    missing = make_error(
        extensions={"retryable": False},
        example={
            "type": "https://example.test/problems/resource_missing",
            "title": "Resource missing",
            "status": 404,
            "code": "resource_missing",
            "retryable": True,
        },
    )
    app, registry = installed_app(missing)
    registry.install(app)

    with pytest.raises(ErrorConfigurationError, match="extension member"):
        app.openapi()


def test_validation_schema_is_not_added_when_normalization_is_disabled() -> None:
    app, registry = installed_app()
    registry.install(app, include_validation_error=False)

    assert "RequestValidationProblem" not in app.openapi()["components"]["schemas"]
