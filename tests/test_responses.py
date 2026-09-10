from dataclasses import FrozenInstanceError

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.responses import RedirectResponse

from fastapi_canon import (
    CanonResponse,
    ErrorRegistry,
)
from fastapi_canon.error import ErrorConfigurationError
from fastapi_canon.error.types import JsonValue, OpenAPIHeader


def registry() -> ErrorRegistry:
    return ErrorRegistry(
        errors=[],
        type_base="https://example.test/problems",
    )


def test_response_is_immutable_and_detaches_schema_and_headers() -> None:
    schema: dict[str, JsonValue] = {"type": "object", "required": ["id"]}
    headers: dict[str, OpenAPIHeader] = {"X-Request-ID": {"schema": {"type": "string"}}}
    response = CanonResponse(
        status=201,
        media_type="application/example+json",
        schema=schema,
        description="Created resource",
        headers=headers,
    )
    schema["type"] = "string"
    headers.clear()

    assert response.as_openapi() == {
        "description": "Created resource",
        "content": {
            "application/example+json": {
                "schema": {"type": "object", "required": ["id"]}
            }
        },
        "headers": {"X-Request-ID": {"schema": {"type": "string"}}},
    }
    with pytest.raises(FrozenInstanceError):
        response.status = 202  # type: ignore[misc]


def test_response_convenience_constructors() -> None:
    assert CanonResponse.json().as_openapi() == {
        "description": "JSON response",
        "content": {"application/json": {"schema": {}}},
    }
    assert CanonResponse.empty().as_openapi() == {"description": "No content"}
    assert CanonResponse.stream("application/x-ndjson").as_openapi() == {
        "description": "Streaming response",
        "content": {
            "application/x-ndjson": {"schema": {"type": "string"}},
        },
    }
    assert CanonResponse.binary(
        "application/pdf", headers=["Content-Disposition"]
    ).as_openapi() == {
        "description": "Binary response",
        "content": {
            "application/pdf": {
                "schema": {"type": "string", "format": "binary"},
            }
        },
        "headers": {
            "Content-Disposition": {"schema": {"type": "string"}},
        },
    }
    assert CanonResponse.sse() == CanonResponse.stream(
        "text/event-stream",
        description="Server-sent event stream",
    )


@pytest.mark.parametrize(
    "response",
    [
        lambda: CanonResponse(status=199, media_type="application/json"),
        lambda: CanonResponse(status=204, media_type="application/json"),
        lambda: CanonResponse(media_type="not-a-media-type"),
        lambda: CanonResponse(media_type=None, schema={"type": "string"}),
        lambda: CanonResponse(media_type="application/json", description=""),
        lambda: CanonResponse.binary("application/pdf", headers=["Content-Type"]),
    ],
)
def test_response_rejects_invalid_contracts(response: object) -> None:
    with pytest.raises(ErrorConfigurationError):
        response()  # type: ignore[operator]


def test_success_contract_replaces_fastapi_default_media_type() -> None:
    errors = registry()
    router = APIRouter()

    @router.get(
        "/events",
        responses=errors.responses(
            http_statuses=[401, 403],
            success=CanonResponse.sse(),
        ),
    )
    async def events() -> None:
        return None

    app = FastAPI()
    app.include_router(router)
    errors.install(app)

    responses = app.openapi()["paths"]["/events"]["get"]["responses"]

    assert responses["200"] == {
        "description": "Server-sent event stream",
        "content": {
            "text/event-stream": {"schema": {"type": "string"}},
        },
    }
    assert "401" in responses
    assert "403" in responses


def test_success_only_route_does_not_require_an_error_registry_declaration() -> None:
    errors = registry()
    router = APIRouter()

    @router.get(
        "/health",
        responses=CanonResponse.json(
            schema={"type": "object"},
            description="Service health",
        ).responses(),
    )
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    app = FastAPI()
    app.include_router(router)
    errors.install(app)

    assert app.openapi()["paths"]["/health"]["get"]["responses"]["200"] == {
        "description": "Service health",
        "content": {"application/json": {"schema": {"type": "object"}}},
    }


def test_empty_success_contract_matches_endpoint_status() -> None:
    errors = registry()
    router = APIRouter()
    router.get(
        "/ready",
        status_code=204,
        responses=errors.responses(success=CanonResponse.empty()),
    )(lambda: None)
    app = FastAPI()
    app.include_router(router)
    errors.install(app)

    assert app.openapi()["paths"]["/ready"]["get"]["responses"] == {
        "204": {"description": "No content"}
    }


def test_empty_redirect_suppresses_fastapi_generated_json_content() -> None:
    errors = registry()
    router = APIRouter()

    @router.get(
        "/elsewhere",
        status_code=307,
        responses=CanonResponse.empty(
            status=307,
            description="Temporary redirect",
        ).responses(),
    )
    async def elsewhere() -> RedirectResponse:
        return RedirectResponse("/target")

    app = FastAPI()
    app.include_router(router)
    errors.install(app)

    assert app.openapi()["paths"]["/elsewhere"]["get"]["responses"]["307"] == {
        "description": "Temporary redirect"
    }


def test_plain_router_contract_does_not_inspect_runtime_response_model() -> None:
    class UserResponse(BaseModel):
        name: str

    errors = registry()
    router = APIRouter()
    router.get(
        "/elsewhere",
        status_code=307,
        response_model=UserResponse,
        responses=CanonResponse.empty(status=307).responses(),
    )(lambda: RedirectResponse("/target"))
    app = FastAPI()
    app.include_router(router)
    errors.install(app)

    assert (
        "content" not in app.openapi()["paths"]["/elsewhere"]["get"]["responses"]["307"]
    )


def test_plain_router_contract_does_not_infer_endpoint_status() -> None:
    errors = registry()
    router = APIRouter()
    router.get(
        "/created",
        responses=errors.responses(success=CanonResponse.json(status=201)),
    )(lambda: None)
    app = FastAPI()
    app.include_router(router)
    errors.install(app)

    responses = app.openapi()["paths"]["/created"]["get"]["responses"]
    assert {"200", "201"} <= responses.keys()
