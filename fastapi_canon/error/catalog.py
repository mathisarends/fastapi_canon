from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from fastapi import FastAPI

from fastapi_canon.error.contracts import INSTALLED_REGISTRY_STATE_KEY
from fastapi_canon.error.openapi import compile_problem_schema
from fastapi_canon.error.problem import Problem
from fastapi_canon.error.registry import AnyError, ErrorRegistry
from fastapi_canon.error.rendering import render_problem_payload
from fastapi_canon.error.types import ErrorConfigurationError

_current_registry: ContextVar[ErrorRegistry | None] = ContextVar(
    "canon_error_registry", default=None
)


def app_registry(app: FastAPI, fallback: ErrorRegistry | None = None) -> ErrorRegistry:
    registry = getattr(app.state, INSTALLED_REGISTRY_STATE_KEY, fallback)
    if not isinstance(registry, ErrorRegistry):
        raise ErrorConfigurationError("application has no installed ErrorRegistry")
    return registry


@contextmanager
def registry_scope(registry: ErrorRegistry | None) -> Iterator[None]:
    token = _current_registry.set(registry)
    try:
        yield
    finally:
        _current_registry.reset(token)


def resolved_registry(fallback: ErrorRegistry) -> ErrorRegistry:
    active = _current_registry.get()
    return active if active is not None else fallback


def problem_for(
    registry: ErrorRegistry,
    exception: Exception,
    *,
    include_status: bool = False,
    instance: str | None = None,
) -> Problem | None:
    if type(include_status) is not bool:
        raise ErrorConfigurationError("include_status must be a bool")
    error = registry.resolve(exception)
    if error is None:
        return None
    type_uri = registry.type_uri_for(error)
    if type_uri is None:
        raise ErrorConfigurationError(
            f"error {error.code!r} has no resolved problem type URI"
        )
    return render_problem_payload(
        error,
        exception,
        type_uri=type_uri,
        include_status=include_status,
        instance=instance,
    )


def problem_schema(
    registry: ErrorRegistry, error: AnyError, *, include_status: bool = False
) -> dict[str, Any]:
    if type(include_status) is not bool:
        raise ErrorConfigurationError("include_status must be a bool")
    schema = compile_problem_schema(registry, error, include_status=include_status)
    schema["$id"] = registry.type_uri_for(error)
    return schema


def internal_problem(registry: ErrorRegistry | None) -> Problem:
    base = registry.type_base if registry is not None else None
    return Problem(
        type=f"{base}/internal_server_error"
        if base
        else "urn:fastapi-canon:internal_server_error",
        title="Internal Server Error",
        code="internal_server_error",
    )
