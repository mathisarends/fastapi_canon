"""Opinionated FastAPI feature composition with Dishka and Problem Details."""

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
from .response import CanonResponse, ResponseConfigurationError, ResponseHeaders
from .router import CanonRouter, CanonRouterGroup

__all__ = [
    "CanonResponse",
    "CanonRouter",
    "CanonRouterGroup",
    "Composition",
    "Error",
    "ErrorConfigurationError",
    "ErrorOptions",
    "ErrorRegistry",
    "ExceptionHandlerSpec",
    "Feature",
    "FeatureConfigurationError",
    "Problem",
    "ProviderFactory",
    "ProviderSource",
    "ResponseConfigurationError",
    "ResponseHeaders",
    "RouterFactory",
    "install_openapi_contracts",
    "validate_openapi_contracts",
]
