from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from fastapi_canon.error import Error, ErrorRegistry


class Missing(Exception):
    pass


def registry(*errors: Error[Any]) -> ErrorRegistry:
    return ErrorRegistry(
        errors=list(errors),
        type_base="https://example.test/problems",
    )


def test_framework_generated_404_and_405_are_problem_details() -> None:
    app = FastAPI()

    @app.get("/existing")
    async def existing() -> None:
        return None

    registry().install(app)
    client = TestClient(app)

    not_found = client.get("/missing")
    method_not_allowed = client.post("/existing")

    assert not_found.status_code == 404
    assert not_found.headers["content-type"] == "application/problem+json"
    assert not_found.json() == {
        "type": "about:blank",
        "title": "Not Found",
        "status": 404,
        "code": "http_404",
        "detail": "Not Found",
    }
    assert method_not_allowed.status_code == 405
    assert method_not_allowed.json()["code"] == "http_405"
    assert method_not_allowed.headers["allow"] == "GET"


def test_declared_error_does_not_change_custom_success_response_class() -> None:
    missing = Error(
        Missing,
        status=404,
        code="resource_missing",
        title="Resource missing",
    )
    errors = registry(missing)
    router = APIRouter()

    @router.get(
        "/text", responses=errors.responses(missing), response_class=PlainTextResponse
    )
    async def text() -> str:
        return "ready"

    app = FastAPI()
    app.include_router(router)
    errors.install(app)
    response = TestClient(app).get("/text")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == "ready"


def test_debug_mode_keeps_starlette_debug_response_precedence() -> None:
    app = FastAPI(debug=True)

    @app.get("/failure")
    async def failure() -> None:
        raise RuntimeError("debug marker")

    registry().install(app)
    response = TestClient(app, raise_server_exceptions=False).get(
        "/failure", headers={"accept": "text/html"}
    )

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("text/html")
    assert "debug marker" in response.text
