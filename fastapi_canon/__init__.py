"""Feature-oriented composition for FastAPI applications."""

from .feature import (
    Composition,
    ExceptionHandlerSpec,
    FaultOptions,
    Feature,
    FeatureConfigurationError,
)

__all__ = [
    "Composition",
    "ExceptionHandlerSpec",
    "FaultOptions",
    "Feature",
    "FeatureConfigurationError",
]
