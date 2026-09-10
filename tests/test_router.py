from collections.abc import Callable
from typing import cast

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from fastapi_canon import (
    CanonResponse,
    CanonRouter,
    Error,
    ErrorConfigurationError,
    ErrorRegistry,
    ResponseConfigurationError,
)


class AuthenticationRequired(Exception):
    pass


class SessionNotFound(Exception):
    pass


class PlaylistEmpty(Exception):
    pass


def error(exception: type[Exception], status: int, code: str) -> Error[Exception]:
    factory = cast("Callable[..., Error[Exception]]", Error)
    return factory(
        exception,
        status=status,
        code=code,
        title=code.replace("_", " ").title(),
    )


AUTHENTICATION_REQUIRED = error(AuthenticationRequired, 401, "authentication_required")
SESSION_NOT_FOUND = error(SessionNotFound, 404, "session_not_found")
PLAYLIST_EMPTY = error(PlaylistEmpty, 409, "playlist_empty")
SESSION_ERRORS = ErrorRegistry(
    name="sessions",
    errors=[AUTHENTICATION_REQUIRED, SESSION_NOT_FOUND, PLAYLIST_EMPTY],
    type_base="https://example.test/problems",
)


def test_router_composes_shared_and_operation_contracts() -> None:
    router = CanonRouter(
        prefix="/sessions",
        error_registry=SESSION_ERRORS,
        raises=[AUTHENTICATION_REQUIRED, SESSION_NOT_FOUND],
    )

    @router.post(
        "/{session_id}/playlist/spotify",
        raises=[PLAYLIST_EMPTY],
        response=CanonResponse.sse(description="Server-sent session events."),
    )
    async def export(session_id: str) -> None:
        del session_id

    app = FastAPI()
    app.include_router(router)
    SESSION_ERRORS.install(app)

    responses = app.openapi()["paths"]["/sessions/{session_id}/playlist/spotify"][
        "post"
    ]["responses"]
    assert set(responses) >= {"200", "401", "404", "409", "422"}
    assert responses["200"] == {
        "description": "Server-sent session events.",
        "content": {"text/event-stream": {"schema": {"type": "string"}}},
    }
    assert responses["401"]["content"]["application/problem+json"]["schema"] == {
        "$ref": "#/components/schemas/AuthenticationRequiredProblem"
    }
    assert responses["409"]["content"]["application/problem+json"]["schema"] == {
        "$ref": "#/components/schemas/PlaylistEmptyProblem"
    }


def test_response_status_becomes_the_route_status() -> None:
    router = CanonRouter()

    @router.get("/elsewhere", response=CanonResponse.empty(status=307))
    async def elsewhere() -> None:
        return None

    app = FastAPI()
    app.include_router(router)

    schema = app.openapi()
    assert schema["paths"]["/elsewhere"]["get"]["responses"] == {
        "307": {"description": "No content"}
    }
    assert "x-fastapi-canon" not in str(schema)


def test_json_response_is_self_contained_without_an_openapi_hook() -> None:
    router = CanonRouter()

    @router.get(
        "/health",
        response=CanonResponse.json(
            schema={"type": "object"},
            description="Service health",
        ),
    )
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    app = FastAPI()
    app.include_router(router)

    schema = app.openapi()
    assert schema["paths"]["/health"]["get"]["responses"]["200"] == {
        "description": "Service health",
        "content": {"application/json": {"schema": {"type": "object"}}},
    }
    assert TestClient(app).get("/health").json() == {"status": "ok"}
    assert "x-fastapi-canon" not in str(schema)


@pytest.mark.parametrize(
    ("method", "verb"),
    [
        ("get", "GET"),
        ("put", "PUT"),
        ("post", "POST"),
        ("delete", "DELETE"),
        ("options", "OPTIONS"),
        ("head", "HEAD"),
        ("patch", "PATCH"),
        ("trace", "TRACE"),
    ],
)
def test_all_http_decorators_accept_canon_contracts(method: str, verb: str) -> None:
    router = CanonRouter()
    decorator = getattr(router, method)
    decorator("/route", response=CanonResponse.json())(lambda: None)

    route = router.routes[0]
    assert isinstance(route, APIRoute)
    assert route.methods == {verb}


def test_api_route_accepts_canon_contracts_for_custom_method_sets() -> None:
    router = CanonRouter(
        error_registry=SESSION_ERRORS,
        raises=[SESSION_NOT_FOUND],
    )
    router.api_route(
        "/resource",
        methods=["GET", "POST"],
        raises=[SESSION_NOT_FOUND],
        response=CanonResponse.json(),
    )(lambda: None)

    route = router.routes[0]
    assert isinstance(route, APIRoute)
    assert route.methods == {"GET", "POST"}
    assert set(route.responses) == {200, 404}


def test_raises_requires_a_registry() -> None:
    with pytest.raises(ErrorConfigurationError, match="requires an error registry"):
        CanonRouter(raises=[SESSION_NOT_FOUND])


def test_error_registry_rejects_invalid_value() -> None:
    with pytest.raises(ErrorConfigurationError, match="error_registry must be"):
        CanonRouter(error_registry=object())  # type: ignore[arg-type]


def test_raises_rejects_foreign_errors_at_declaration_time() -> None:
    foreign = error(SessionNotFound, 404, "foreign_session")
    router = CanonRouter(error_registry=SESSION_ERRORS)

    with pytest.raises(ErrorConfigurationError, match="does not belong"):
        router.get("/session", raises=[foreign])


def test_response_rejects_mismatched_status_at_declaration_time() -> None:
    router = CanonRouter()

    with pytest.raises(ResponseConfigurationError, match="status_code is 200"):
        router.get(
            "/created",
            status_code=200,
            response=CanonResponse.json(status=201),
        )


def test_bodyless_response_rejects_explicit_model_at_declaration_time() -> None:
    router = CanonRouter()

    with pytest.raises(ResponseConfigurationError, match="response_model conflicts"):
        router.get(
            "/elsewhere",
            response_model=dict[str, str],
            response=CanonResponse.empty(status=307),
        )


def test_canon_contract_rejects_conflicting_fastapi_response() -> None:
    router = CanonRouter()

    with pytest.raises(ResponseConfigurationError, match="conflicts with a FastAPI"):
        router.get(
            "/resource",
            response=CanonResponse.json(),
            responses={200: {"description": "Manual response"}},
        )


def test_nested_reused_router_preserves_contracts_and_streaming_runtime() -> None:
    router = CanonRouter(
        error_registry=SESSION_ERRORS,
        raises=[SESSION_NOT_FOUND],
    )

    @router.get("/{session_id}", response=CanonResponse.sse())
    async def events(session_id: str) -> StreamingResponse:
        return StreamingResponse(
            iter([f"data: {session_id}\n\n"]), media_type="text/event-stream"
        )

    parent = APIRouter(prefix="/sessions")
    parent.include_router(router)
    app = FastAPI()
    app.include_router(parent, prefix="/one")
    app.include_router(parent, prefix="/two")
    SESSION_ERRORS.install(app)

    schema = app.openapi()
    with TestClient(app) as client:
        for prefix in ("/one", "/two"):
            response = client.get(f"{prefix}/sessions/123")
            assert response.text == "data: 123\n\n"
            assert response.headers["content-type"].startswith("text/event-stream")
            responses = schema["paths"][f"{prefix}/sessions/{{session_id}}"]["get"][
                "responses"
            ]
            assert set(responses["200"]["content"]) == {"text/event-stream"}
            assert "404" in responses
            assert "x-fastapi-canon" not in str(responses)


def test_hidden_routes_still_validate_canon_declarations() -> None:
    router = CanonRouter()
    with pytest.raises(ResponseConfigurationError, match="status_code is 200"):
        router.get(
            "/hidden",
            include_in_schema=False,
            response=CanonResponse.json(status=201),
            status_code=200,
        )
