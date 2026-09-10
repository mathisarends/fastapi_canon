from collections.abc import Mapping
from typing import Any, cast

from pydantic import BaseModel, ValidationError

from fastapi_canon.error.error import Error, freeze_headers
from fastapi_canon.error.problem import Problem
from fastapi_canon.error.types import ErrorConfigurationError, JsonValue

type AnyError = Error[Any]

_RESERVED_MEMBERS = frozenset({"type", "title", "status", "detail", "instance", "code"})


def render_problem(
    error: AnyError, exception: Exception, *, type_uri: str
) -> tuple[Problem, dict[str, str]]:
    """Render and validate one registered error occurrence atomically."""
    if not isinstance(exception, error.exception):
        msg = (
            f"cannot render {type(exception).__qualname__} with error for "
            f"{error.exception.__qualname__}"
        )
        raise ErrorConfigurationError(msg)

    detail = _render_detail(error, exception)
    extensions = _render_extensions(error, exception)
    overlap = _RESERVED_MEMBERS.intersection(extensions)
    if overlap:
        names = ", ".join(sorted(overlap))
        msg = f"rendered extensions must not redefine reserved members: {names}"
        raise ErrorConfigurationError(msg)

    payload: dict[str, object] = {
        "type": type_uri,
        "title": error.title,
        "status": error.status,
        "code": error.code,
        "detail": detail,
        **extensions,
    }
    try:
        problem = Problem.model_validate(payload)
    except ValidationError as validation_error:
        msg = f"error {error.code!r} rendered an invalid Problem Details payload"
        raise ErrorConfigurationError(msg) from validation_error

    return problem, _render_headers(error, exception)


def _render_detail(error: AnyError, exception: Exception) -> str | None:
    renderer: object = error.detail
    rendered: object = renderer(exception) if callable(renderer) else renderer
    if rendered is not None and not isinstance(rendered, str):
        msg = "detail callback must return a string or None"
        raise ErrorConfigurationError(msg)
    return rendered


def _render_extensions(error: AnyError, exception: Exception) -> dict[str, JsonValue]:
    renderer: object = error.extensions
    rendered: object = renderer(exception) if callable(renderer) else renderer
    if rendered is None:
        return {}
    if isinstance(rendered, BaseModel):
        rendered = rendered.model_dump(mode="python", by_alias=False)
    if not isinstance(rendered, Mapping):
        msg = "extensions callback must return a mapping or BaseModel"
        raise ErrorConfigurationError(msg)

    model = error.extensions_model
    if model is not None:
        try:
            rendered_model = model.model_validate(dict(rendered))
        except ValidationError as validation_error:
            msg = "rendered extensions do not validate against extensions_model"
            raise ErrorConfigurationError(msg) from validation_error
        rendered = rendered_model.model_dump(mode="json", by_alias=True)

    return cast(dict[str, JsonValue], dict(rendered))


def _render_headers(error: AnyError, exception: Exception) -> dict[str, str]:
    renderer: object = error.headers
    rendered: object = renderer(exception) if callable(renderer) else renderer
    if rendered is None:
        return {}
    if not isinstance(rendered, Mapping):
        msg = "headers callback must return a mapping"
        raise ErrorConfigurationError(msg)
    return dict(freeze_headers(rendered, path="rendered headers"))
