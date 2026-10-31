from typing import Any

import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient
from openapi_spec_validator import validate

from fastapi_canon.error import Error, ErrorConfigurationError, ErrorRegistry


class Missing(Exception):
    pass


def make_registry(*errors: Error[Any]) -> ErrorRegistry:
    return ErrorRegistry(
        errors=list(errors),
        type_base="https://example.test/problems",
    )


def test_generic_http_responses_match_the_runtime_handler() -> None:
    registry = make_registry()
    router = APIRouter()

    @router.get(
        "/protected",
        responses=registry.responses(http_statuses=[401, 403]),
    )
    async def protected() -> None:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    app = FastAPI()
    app.include_router(router)
    registry.install(app)
    document = app.openapi()
    validate(document)

    documented = document["paths"]["/protected"]["get"]["responses"]
    assert documented["401"]["content"]["application/problem+json"]["schema"] == {
        "$ref": "#/components/schemas/Http401Problem"
    }
    assert documented["403"]["content"]["application/problem+json"]["schema"] == {
        "$ref": "#/components/schemas/Http403Problem"
    }
    assert "x-fastapi-canon-http-statuses" not in documented["401"]
    assert document["components"]["schemas"]["Http401Problem"]["properties"] == {
        "type": {
            "type": "string",
            "format": "uri-reference",
            "const": "about:blank",
        },
        "title": {"type": "string", "const": "Unauthorized"},
        "status": {"type": "integer", "const": 401},
        "code": {"type": "string", "const": "http_401"},
        "detail": {"type": ["string", "null"]},
        "instance": {"type": ["string", "null"], "format": "uri-reference"},
    }

    response = TestClient(app).get("/protected")

    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {
        "type": "about:blank",
        "title": "Unauthorized",
        "status": 401,
        "code": "http_401",
        "detail": "Authentication required",
    }


def test_domain_and_generic_http_problem_can_share_a_status() -> None:
    missing = Error(
        Missing,
        status=404,
        code="resource_missing",
        title="Resource missing",
    )
    registry = make_registry(missing)
    router = APIRouter()
    router.get(
        "/resource",
        responses=registry.responses(missing, http_statuses=[404]),
    )(lambda: None)
    app = FastAPI()
    app.include_router(router)
    registry.install(app)

    schema = app.openapi()["paths"]["/resource"]["get"]["responses"]["404"]["content"][
        "application/problem+json"
    ]["schema"]

    assert schema["oneOf"] == [
        {"$ref": "#/components/schemas/ResourceMissingProblem"},
        {"$ref": "#/components/schemas/Http404Problem"},
    ]
    assert schema["discriminator"]["mapping"] == {
        "resource_missing": "#/components/schemas/ResourceMissingProblem",
        "http_404": "#/components/schemas/Http404Problem",
    }


@pytest.mark.parametrize("statuses", [[399], [600], [True], [401, 401]])
def test_generic_http_responses_reject_invalid_statuses(statuses: list[int]) -> None:
    with pytest.raises(ErrorConfigurationError, match="http_statuses"):
        make_registry().responses(http_statuses=statuses)


def test_declared_http_response_requires_runtime_normalization() -> None:
    registry = make_registry()
    router = APIRouter()
    router.get(
        "/protected",
        responses=registry.responses(http_statuses=[401]),
    )(lambda: None)
    app = FastAPI()
    app.include_router(router)

    registry.install(app, include_http_exceptions=False)
    with pytest.raises(ErrorConfigurationError, match="normalization is disabled"):
        app.openapi()


def test_generic_http_response_rejects_a_conflicting_domain_code() -> None:
    conflict = Error(
        Missing,
        status=404,
        code="http_404",
        title="Custom missing response",
    )
    registry = make_registry(conflict)

    with pytest.raises(ErrorConfigurationError, match="conflicts with domain error"):
        registry.responses(conflict, http_statuses=[404])
