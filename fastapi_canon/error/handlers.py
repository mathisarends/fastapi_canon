from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.types import ExceptionHandler

from fastapi_canon.error.contracts import (
    INSTALLED_REGISTRY_STATE_KEY,
    iter_http_contracts,
)
from fastapi_canon.error.openapi import install_openapi
from fastapi_canon.error.rendering import render_problem
from fastapi_canon.error.types import (
    ErrorConfigurationError,
    JsonValue,
    http_problem_title,
)

if TYPE_CHECKING:
    from fastapi_canon.error.registry import ErrorRegistry

logger = logging.getLogger(__name__)


def install_handlers(
    registry: ErrorRegistry,
    app: FastAPI,
    *,
    include_validation_error: bool,
    include_http_exceptions: bool,
    include_unhandled_error: bool,
) -> None:
    """Install handlers once while preserving user-owned handler conflicts."""
    installed = getattr(app.state, INSTALLED_REGISTRY_STATE_KEY, None)
    if installed is registry:
        return
    if installed is not None:
        msg = "a different ErrorRegistry is already installed on this application"
        raise ErrorConfigurationError(msg)
    if app.openapi_schema is not None:
        msg = "install ErrorRegistry before generating or caching OpenAPI"
        raise ErrorConfigurationError(msg)

    registry.require_resolved()
    _validate_route_contracts(
        registry,
        app,
        include_http_exceptions=include_http_exceptions,
    )
    if (
        include_validation_error or include_unhandled_error
    ) and registry.type_base is None:
        msg = (
            "type_base is required when validation or unhandled-error normalization "
            "is enabled"
        )
        raise ErrorConfigurationError(msg)

    domain_classes = {
        *(error.exception for error in registry.errors),
    }
    for exception_class in domain_classes:
        _ensure_handler_available(app, exception_class)

    domain_handler = _domain_handler(registry)
    for exception_class in domain_classes:
        app.add_exception_handler(exception_class, domain_handler)

    if include_http_exceptions:
        _ensure_default_or_available(app, HTTPException, http_exception_handler)
        app.add_exception_handler(HTTPException, _http_exception_handler)
    if include_validation_error:
        _ensure_default_or_available(
            app, RequestValidationError, request_validation_exception_handler
        )
        app.add_exception_handler(
            RequestValidationError, _request_validation_handler(registry)
        )
        _ensure_handler_available(app, ResponseValidationError)
        app.add_exception_handler(
            ResponseValidationError, _response_validation_handler(registry)
        )
    if include_unhandled_error:
        _ensure_handler_available(app, Exception)
        app.add_exception_handler(Exception, _unhandled_handler(registry))

    install_openapi(
        registry,
        app,
        include_validation_error=include_validation_error,
        include_http_exceptions=include_http_exceptions,
    )
    setattr(app.state, INSTALLED_REGISTRY_STATE_KEY, registry)


def _validate_route_contracts(
    registry: ErrorRegistry, app: FastAPI, *, include_http_exceptions: bool = True
) -> None:
    for http_route, http_errors, http_statuses in iter_http_contracts(
        app.router, registry
    ):
        for http_error in http_errors:
            if not registry.contains(http_error):
                msg = (
                    f"route {http_route.path!r} declares error "
                    f"{http_error.code!r}, but the "
                    "installed registry does not contain that exact definition"
                )
                raise ErrorConfigurationError(msg)
        if http_statuses and not include_http_exceptions:
            statuses = ", ".join(str(status) for status in http_statuses)
            msg = (
                f"route {http_route.path!r} declares normalized HTTP responses "
                f"for {statuses}, but HTTP exception normalization is disabled"
            )
            raise ErrorConfigurationError(msg)


def _domain_handler(registry: ErrorRegistry) -> ExceptionHandler:
    async def handler(connection: Request, exception: Exception) -> Response:
        error = registry.resolve(exception)
        if error is None:
            raise exception
        type_uri = registry.type_uri_for(error)
        if type_uri is None:
            msg = f"error {error.code!r} has no resolved problem type URI"
            raise ErrorConfigurationError(msg)
        try:
            problem, headers = render_problem(error, exception, type_uri=type_uri)
        except Exception:
            logger.exception(
                "Error rendering callback failed",
                extra={
                    "code": error.code,
                    "exception_class": type(exception).__qualname__,
                },
            )
            return _internal_error_response(registry)
        return _problem_response(problem.as_dict(), error.status, headers=headers)

    return cast(ExceptionHandler, handler)


async def _http_exception_handler(request: Request, exception: Exception) -> Response:
    del request
    if not isinstance(exception, HTTPException):
        raise exception
    status = exception.status_code
    title = http_problem_title(status)
    detail = exception.detail if isinstance(exception.detail, str) else None
    payload: dict[str, JsonValue] = {
        "type": "about:blank",
        "title": title,
        "status": status,
        "code": f"http_{status}",
    }
    if detail is not None:
        payload["detail"] = detail
    return _problem_response(payload, status, headers=exception.headers)


def _request_validation_handler(registry: ErrorRegistry) -> ExceptionHandler:
    async def handler(request: Request, exception: Exception) -> Response:
        del request
        if not isinstance(exception, RequestValidationError):
            raise exception
        errors: list[JsonValue] = []
        for error in exception.errors():
            errors.append(_validation_error(error))
        payload: dict[str, JsonValue] = {
            "type": _builtin_type(registry, "request_validation_error"),
            "title": "Request validation failed",
            "status": 422,
            "code": "request_validation_error",
            "errors": errors,
        }
        return _problem_response(payload, 422)

    return cast(ExceptionHandler, handler)


def _response_validation_handler(registry: ErrorRegistry) -> ExceptionHandler:
    async def handler(request: Request, exception: Exception) -> Response:
        del request
        if not isinstance(exception, ResponseValidationError):
            raise exception
        logger.error(
            "FastAPI response validation failed",
            exc_info=(type(exception), exception, exception.__traceback__),
        )
        return _internal_error_response(registry)

    return cast(ExceptionHandler, handler)


def _unhandled_handler(registry: ErrorRegistry) -> ExceptionHandler:
    async def handler(request: Request, exception: Exception) -> Response:
        del request
        logger.error(
            "Unhandled application exception",
            exc_info=(type(exception), exception, exception.__traceback__),
        )
        return _internal_error_response(registry)

    return cast(ExceptionHandler, handler)


def _validation_error(error: dict[str, object]) -> JsonValue:
    location = error.get("loc")
    parts = tuple(location) if isinstance(location, tuple | list) else ()
    source = parts[0] if parts else None
    result: dict[str, JsonValue] = {
        "code": str(error.get("type", "validation_error")),
        "detail": str(error.get("msg", "Invalid input")),
    }
    if source == "body":
        pointer = "#/" + "/".join(_escape_pointer(part) for part in parts[1:])
        result["pointer"] = pointer.rstrip("/") or "#"
    elif source in {"path", "query", "header", "cookie"} and len(parts) > 1:
        result["parameter"] = str(parts[-1])
        result["in"] = cast(str, source)
    return result


def _escape_pointer(value: object) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


def _internal_error_response(registry: ErrorRegistry) -> Response:
    return _problem_response(
        {
            "type": _builtin_type(registry, "internal_server_error"),
            "title": "Internal Server Error",
            "status": 500,
            "code": "internal_server_error",
        },
        500,
    )


def _builtin_type(registry: ErrorRegistry, code: str) -> str:
    if registry.type_base is None:
        msg = f"type_base is required to render built-in problem {code!r}"
        raise ErrorConfigurationError(msg)
    return f"{registry.type_base}/{code}"


def _problem_response(
    payload: dict[str, JsonValue],
    status: int,
    *,
    headers: Mapping[str, str] | None = None,
) -> Response:
    return JSONResponse(
        payload,
        status_code=status,
        headers=headers,
        media_type="application/problem+json",
    )


def _ensure_handler_available(app: FastAPI, exception_class: type[Exception]) -> None:
    if exception_class in app.exception_handlers:
        msg = (
            f"application already defines an exception handler for "
            f"{exception_class.__qualname__}"
        )
        raise ErrorConfigurationError(msg)


def _ensure_default_or_available(
    app: FastAPI,
    exception_class: type[Exception],
    known_default: object,
) -> None:
    existing = app.exception_handlers.get(exception_class)
    if existing is not None and existing is not known_default:
        msg = (
            f"application already defines an exception handler for "
            f"{exception_class.__qualname__}"
        )
        raise ErrorConfigurationError(msg)
