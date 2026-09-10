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
from .response import CanonResponse, ResponseConfigurationError, ResponseHeaders

__all__ = [
    "CanonResponse",
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
]
