from typing import Annotated, Any

import pytest
from fastapi import Cookie, FastAPI, Header, Path, Query
from fastapi.testclient import TestClient
from pydantic import BaseModel

from fastapi_canon.error import ErrorRegistry


class Body(BaseModel):
    count: int


def make_app() -> FastAPI:
    app = FastAPI()

    @app.post("/body")
    async def body(payload: Body) -> None:
        del payload

    @app.get("/path/{item_id}")
    async def path(item_id: Annotated[int, Path()]) -> None:
        del item_id

    @app.get("/query")
    async def query(limit: Annotated[int, Query()]) -> None:
        del limit

    @app.get("/header")
    async def header(request_id: Annotated[str, Header(alias="X-Request-ID")]) -> None:
        del request_id

    @app.get("/cookie")
    async def cookie(session: Annotated[str, Cookie()]) -> None:
        del session

    ErrorRegistry(errors=[], type_base="https://example.test/problems").install(app)
    return app


@pytest.mark.parametrize(
    ("method", "path", "kwargs", "expected"),
    [
        (
            "post",
            "/body",
            {"json": {"count": "private input"}},
            {"pointer": "#/count", "code": "int_parsing"},
        ),
        (
            "get",
            "/path/not-an-int",
            {},
            {"parameter": "item_id", "in": "path", "code": "int_parsing"},
        ),
        (
            "get",
            "/query",
            {},
            {"parameter": "limit", "in": "query", "code": "missing"},
        ),
        (
            "get",
            "/header",
            {},
            {"parameter": "X-Request-ID", "in": "header", "code": "missing"},
        ),
        (
            "get",
            "/cookie",
            {},
            {"parameter": "session", "in": "cookie", "code": "missing"},
        ),
    ],
)
def test_validation_location_is_stable_and_private_input_is_omitted(
    method: str,
    path: str,
    kwargs: dict[str, Any],
    expected: dict[str, str],
) -> None:
    response = TestClient(make_app()).request(method, path, **kwargs)
    error = response.json()["errors"][0]

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert {name: error[name] for name in expected} == expected
    assert "input" not in error
    assert "private input" not in response.text
