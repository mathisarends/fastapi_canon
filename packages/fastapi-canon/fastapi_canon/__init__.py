"""Opinionated FastAPI feature composition with Dishka and Problem Details."""

from http import HTTPMethod

from ._version import __version__
from .error import Error, ErrorConfigurationError, ErrorRegistry, Problem
from .feature import (
    Composition,
    ErrorOptions,
    ExceptionHandlerSpec,
    Feature,
    FeatureConfigurationError,
    ProviderFactory,
    ProviderSource,
    RouterFactory,
)
from .openapi import install_openapi_contracts, validate_openapi_contracts
from .policy import RouterPolicy
from .response import CanonResponse, ResponseConfigurationError, ResponseHeaders
from .router import CanonRouter

__all__ = [
    "CanonResponse",
    "CanonRouter",
    "Composition",
    "Error",
    "ErrorConfigurationError",
    "ErrorOptions",
    "ErrorRegistry",
    "ExceptionHandlerSpec",
    "Feature",
    "FeatureConfigurationError",
    "HTTPMethod",
    "Problem",
    "ProviderFactory",
    "ProviderSource",
    "ResponseConfigurationError",
    "ResponseHeaders",
    "RouterFactory",
    "RouterPolicy",
    "__version__",
    "install_openapi_contracts",
    "validate_openapi_contracts",
]
