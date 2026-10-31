"""The same registered problem rendered and described for different bindings."""

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ValidationError

from fastapi_canon import (
    CanonRouter,
    Composition,
    Error,
    ErrorConfigurationError,
    ErrorOptions,
    ErrorRegistry,
    Feature,
)
from fastapi_canon.error.catalog import app_registry, problem_for, problem_schema


class Missing(Exception):
    pass


class Expired(Missing):
    pass


class Unknown(Exception):
    pass


def test_http_and_embedded_problems_share_identity_and_public_data() -> None:
    def headers(exception: Missing) -> dict[str, str]:
        assert isinstance(exception, Expired)
        return {"X-Problem": "missing"}

    missing = Error(
        Missing,
        status=404,
        code="item_missing",
        title="Item missing",
        detail=lambda _exception: "Choose another item.",
        extensions={"replacement": None},
        headers=headers,
    )
    registry = ErrorRegistry(
        errors=[missing], type_base="https://api.example.test/problems"
    )
    router = CanonRouter(error_registry=registry)

    @router.get("/item", raises=[missing])
    async def item() -> None:
        raise Expired("private database information")

    app = FastAPI()
    app.include_router(router)
    registry.install(app)
    with TestClient(app) as client:
        response = client.get("/item")
    assert response.status_code == 404
    assert response.headers["X-Problem"] == "missing"

    embedded = problem_for(registry, Expired("private database information"))
    assert embedded is not None
    assert embedded.status is None
    payload = embedded.as_dict()
    assert response.json() == {**payload, "status": 404}
    assert payload["replacement"] is None
    assert "private" not in response.text
    assert "status" not in payload
    Draft202012Validator(problem_schema(registry, missing)).validate(payload)
    Draft202012Validator(
        problem_schema(registry, missing, include_status=True)
    ).validate(response.json())


def test_embedded_rendering_does_not_evaluate_http_headers() -> None:
    def headers(_exception: Missing) -> dict[str, str]:
        pytest.fail("embedded problems must not evaluate HTTP response headers")

    missing = Error(
        Missing, status=404, code="item_missing", title="Item missing", headers=headers
    )
    registry = ErrorRegistry(errors=[missing], type_base="urn:example:problems")
    problem = problem_for(registry, Missing(), instance="urn:uuid:1234")
    assert problem is not None
    assert problem.instance == "urn:uuid:1234"
    assert problem.as_dict()["type"] == "urn:example:problems/item_missing"


def test_application_catalog_reuses_composition_resolution() -> None:
    missing = Error(Missing, status=404, code="item_missing", title="Item missing")
    local = ErrorRegistry(errors=[missing])
    app = Composition(
        Feature(name="items", errors=local),
        errors=ErrorOptions(type_base="https://api.example.test/problems"),
    ).apply(FastAPI())
    registry = app_registry(app)
    assert registry.errors == local.errors
    problem = problem_for(registry, Missing())
    assert problem is not None
    assert problem.type == "https://api.example.test/problems/item_missing"
    assert problem_schema(registry, missing)["$id"] == problem.type
    assert app_registry(app) is registry
    with pytest.raises(ErrorConfigurationError, match="no installed ErrorRegistry"):
        app_registry(FastAPI())


def test_registry_reports_unmapped_and_unresolved_problems_explicitly() -> None:
    missing = Error(Missing, status=404, code="item_missing", title="Item missing")
    registry = ErrorRegistry(errors=[missing])
    assert problem_for(registry, Unknown()) is None
    with pytest.raises(ErrorConfigurationError, match="resolved problem type URI"):
        problem_for(registry, Missing())
    with pytest.raises(ErrorConfigurationError, match="resolved problem type URI"):
        problem_schema(registry, missing)
    foreign = Error(Unknown, status=404, code="unknown_item", title="Unknown item")
    with pytest.raises(ErrorConfigurationError, match="does not belong"):
        problem_schema(registry, foreign)


@pytest.mark.parametrize("include_status", [True, False])
def test_shared_schema_keeps_nested_extension_references_local(
    include_status: bool,
) -> None:
    class Replacement(BaseModel):
        identifier: str

    class Fields(BaseModel):
        replacement: Replacement | None

    missing = Error(
        Missing,
        status=404,
        code="item_missing",
        title="Item missing",
        extensions_model=Fields,
        extensions=lambda _error: Fields(replacement=Replacement(identifier="new")),
        example={
            "type": "urn:example:missing",
            "title": "Item missing",
            "status": 404,
            "code": "item_missing",
            "replacement": {"identifier": "new"},
        },
        type="urn:example:missing",
    )
    registry = ErrorRegistry(errors=[missing])
    schema = problem_schema(registry, missing, include_status=include_status)
    problem = problem_for(registry, Missing(), include_status=include_status)
    assert problem is not None
    assert schema["$id"] == "urn:example:missing"
    assert "#/components/" not in str(schema)
    assert "#/$defs/Replacement" in str(schema)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    validator.validate(problem.as_dict())
    validator.validate(schema["examples"][0])
    schema["properties"].clear()
    assert problem_schema(registry, missing, include_status=include_status)[
        "properties"
    ]


@pytest.mark.parametrize("value", [1, None, "true"])
def test_problem_binding_options_require_booleans(value: Any) -> None:
    registry = ErrorRegistry(errors=[])
    with pytest.raises(ErrorConfigurationError, match="include_status"):
        problem_for(registry, Missing(), include_status=value)
    missing = Error(Missing, status=404, code="item_missing", title="Item missing")
    with pytest.raises(ErrorConfigurationError, match="include_status"):
        problem_schema(registry, missing, include_status=value)


def test_problem_instance_validation_is_shared_with_the_public_model() -> None:
    missing = Error(
        Missing,
        status=404,
        code="item_missing",
        title="Item missing",
        type="urn:example:missing",
    )
    registry = ErrorRegistry(errors=[missing])
    with pytest.raises(
        ErrorConfigurationError, match="invalid Problem Details payload"
    ):
        problem_for(registry, Missing(), instance="contains whitespace")


def test_same_status_problem_union_uses_resolved_type_uris() -> None:
    missing = Error(Missing, status=404, code="item_missing", title="Item missing")
    expired = Error(Expired, status=404, code="item_expired", title="Item expired")
    local = ErrorRegistry(errors=[missing, expired])
    router = CanonRouter(error_registry=local)
    router.get("/item", raises=[missing, expired])(lambda: None)
    registry = ErrorRegistry.merge(local, type_base="urn:example:problems")
    app = FastAPI()
    app.include_router(router)
    registry.install(app)
    schema = app.openapi()["paths"]["/item"]["get"]["responses"]["404"]["content"][
        "application/problem+json"
    ]["schema"]
    assert schema["discriminator"] == {
        "propertyName": "type",
        "mapping": {
            "urn:example:problems/item_missing": "#/components/schemas/ItemMissingProblem",
            "urn:example:problems/item_expired": "#/components/schemas/ItemExpiredProblem",
        },
    }


def test_ambiguous_about_blank_union_fails_without_caching() -> None:
    missing = Error(
        Missing, status=404, code="item_missing", title="Not Found", type="about:blank"
    )
    registry = ErrorRegistry(errors=[missing], type_base="urn:example:problems")
    app = FastAPI()
    app.get("/item", responses=registry.responses(missing, http_statuses=[404]))(
        lambda: None
    )
    registry.install(app)
    with pytest.raises(
        ErrorConfigurationError, match="ambiguous problem type 'about:blank'"
    ):
        app.openapi()
    assert app.openapi_schema is None


@pytest.mark.parametrize("status", [True, False, "404", 404.0])
def test_problem_status_is_an_actual_json_integer(status: Any) -> None:
    from fastapi_canon import Problem

    with pytest.raises(ValidationError):
        Problem(
            type="urn:example:missing",
            title="Item missing",
            code="item_missing",
            status=status,
        )
