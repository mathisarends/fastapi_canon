from fastapi import FastAPI

from fastapi_canon.error.openapi import install_openapi
from fastapi_canon.error.types import ErrorConfigurationError


def install_openapi_contracts(app: FastAPI) -> None:
    """Install success-contract OpenAPI compilation on a FastAPI application."""
    _validate_app(app)
    if app.openapi_schema is not None:
        msg = "install OpenAPI contracts before generating or caching OpenAPI"
        raise ErrorConfigurationError(msg)
    install_openapi(
        None,
        app,
        include_validation_error=False,
        include_http_exceptions=False,
    )


def validate_openapi_contracts(app: FastAPI) -> None:
    """Generate OpenAPI now so all installed canon contracts are validated."""
    _validate_app(app)
    app.openapi()


def _validate_app(app: object) -> None:
    raw_app: object = app
    if not isinstance(raw_app, FastAPI):
        msg = "app must be a FastAPI instance"
        raise ErrorConfigurationError(msg)
