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
from .response import Response, ResponseHeaders

__all__ = [
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
    "Response",
    "ResponseHeaders",
    "RouterFactory",
]
