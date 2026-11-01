from typing import Any, cast

import pytest
from fastapi import FastAPI

from fastapi_canon import CanonResponse, ErrorConfigurationError, ErrorRegistry


def test_custom_openapi_is_compiled_without_matching_router_objects() -> None:
    document: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {"title": "Custom", "version": "1"},
        "paths": {
            "/external": {
                "summary": "Preserved",
                "get": {
                    "responses": {
                        str(status): response
                        for status, response in CanonResponse.empty(status=307)
                        .responses()
                        .items()
                    }
                },
            }
        },
    }
    app = FastAPI()
    cast(Any, app).openapi = lambda: document
    registry = ErrorRegistry(errors=[], type_base="https://example.test/problems")
    registry.install(app)

    compiled = app.openapi()
    assert compiled["info"] == document["info"]
    assert compiled["paths"]["/external"]["summary"] == "Preserved"
    assert compiled["paths"]["/external"]["get"]["responses"]["307"] == {
        "description": "No content"
    }
    assert "x-fastapi-canon-success" in str(document)
    assert app.openapi() is compiled


def test_failed_compilation_does_not_cache_unvalidated_schema() -> None:
    app = FastAPI()
    app.get(
        "/invalid",
        responses={400: {"x-fastapi-canon-errors": ["unknown"]}},
    )(lambda: None)
    ErrorRegistry(errors=[], type_base="https://example.test/problems").install(app)

    for _ in range(2):
        with pytest.raises(
            ErrorConfigurationError, match="exact definition is missing"
        ):
            app.openapi()
        assert app.openapi_schema is None


def test_hidden_plain_routes_are_outside_document_validation() -> None:
    app = FastAPI()
    app.get(
        "/hidden",
        include_in_schema=False,
        responses={400: {"x-fastapi-canon-errors": ["unknown"]}},
    )(lambda: None)
    ErrorRegistry(errors=[], type_base="https://example.test/problems").install(app)
    assert app.openapi()["paths"] == {}
