from collections.abc import Callable
from typing import cast
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict

from fastapi_canon.error import Error, ErrorConfigurationError
from fastapi_canon.error.rendering import render_problem


class SessionNotFound(Exception):
    def __init__(self, session_id: UUID) -> None:
        self.session_id = session_id


class OtherError(Exception):
    pass


class SessionFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: UUID


def make_error(**overrides: object) -> Error[SessionNotFound]:
    values: dict[str, object] = {
        "status": 404,
        "code": "session_not_found",
        "title": "Session not found",
    }
    values.update(overrides)
    factory = cast("Callable[..., Error[SessionNotFound]]", Error)
    return factory(SessionNotFound, **values)


def test_render_problem_serializes_typed_extensions_and_headers() -> None:
    session_id = UUID("0198e2ef-799c-765d-9ab4-2130d11d1e70")
    error = make_error(
        detail=lambda exception: f"Session {exception.session_id} does not exist.",
        extensions_model=SessionFields,
        extensions=lambda exception: SessionFields(session_id=exception.session_id),
        headers=lambda _exception: {"Retry-After": "10"},
    )

    problem, headers = render_problem(
        error,
        SessionNotFound(session_id),
        type_uri="https://api.example.com/problems/session_not_found",
    )

    assert problem.as_dict() == {
        "type": "https://api.example.com/problems/session_not_found",
        "title": "Session not found",
        "status": 404,
        "code": "session_not_found",
        "detail": f"Session {session_id} does not exist.",
        "session_id": str(session_id),
    }
    assert headers == {"Retry-After": "10"}


def test_render_problem_handles_empty_optional_renderers() -> None:
    problem, headers = render_problem(
        make_error(),
        SessionNotFound(UUID(int=0)),
        type_uri="urn:example:session-not-found",
    )

    assert "detail" not in problem.as_dict()
    assert headers == {}


def test_render_problem_accepts_static_extensions() -> None:
    problem, _ = render_problem(
        make_error(extensions={"attempts": [1, 2]}),
        SessionNotFound(UUID(int=0)),
        type_uri="urn:example:session-not-found",
    )

    assert problem.as_dict()["attempts"] == [1, 2]


def test_render_problem_rejects_wrong_exception() -> None:
    with pytest.raises(ErrorConfigurationError):
        render_problem(
            make_error(),
            OtherError(),
            type_uri="urn:example:session-not-found",
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"detail": cast("Callable[[SessionNotFound], str]", lambda _: 42)},
        {
            "extensions_model": SessionFields,
            "extensions": lambda _: {"session_id": "invalid"},
        },
        {
            "extensions_model": SessionFields,
            "extensions": cast("Callable[[SessionNotFound], object]", lambda _: 42),
        },
        {"headers": cast("Callable[[SessionNotFound], object]", lambda _: 42)},
        {"headers": lambda _: {"Content-Type": "text/plain"}},
    ],
)
def test_render_problem_rejects_invalid_callback_results(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ErrorConfigurationError):
        render_problem(
            make_error(**overrides),
            SessionNotFound(UUID(int=0)),
            type_uri="urn:example:session-not-found",
        )


def test_render_problem_rejects_reserved_members_from_permissive_model() -> None:
    class PermissiveFields(BaseModel):
        model_config = ConfigDict(extra="allow")

    error = make_error(
        extensions_model=PermissiveFields,
        extensions=lambda _: {"detail": "unsafe"},
    )

    with pytest.raises(ErrorConfigurationError, match="reserved"):
        render_problem(
            error,
            SessionNotFound(UUID(int=0)),
            type_uri="urn:example:session-not-found",
        )


def test_render_problem_wraps_problem_validation_failure() -> None:
    with pytest.raises(ErrorConfigurationError) as error:
        render_problem(
            make_error(),
            SessionNotFound(UUID(int=0)),
            type_uri="not a URI",
        )

    assert error.value.__cause__ is not None
