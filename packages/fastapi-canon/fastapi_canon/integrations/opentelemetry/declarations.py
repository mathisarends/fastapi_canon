"""Optional telemetry declarations; importing this module needs no OTEL packages."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from starlette.types import Scope as ASGIScope

from fastapi_canon.error.types import FeatureConfigurationError

if TYPE_CHECKING:
    from dishka import Provider
    from fastapi import FastAPI
    from opentelemetry._logs import LoggerProvider
    from opentelemetry.metrics import MeterProvider
    from opentelemetry.trace import TracerProvider

    from fastapi_canon.integrations.websockets.adapters.opentelemetry import (
        OpenTelemetry,
    )

__all__ = ["Instrumentation", "Telemetry", "instrument"]


@dataclass(frozen=True, slots=True, init=False)
class Instrumentation:
    """An immutable feature declaration returned by :func:`instrument`."""

    port: type[Any]
    expected: tuple[type[Exception], ...]
    component: str

    def __init__(
        self,
        *,
        port: type[Any],
        expected: Sequence[type[Exception]] = (),
        component: str = "",
    ) -> None:
        if not isinstance(port, type):
            raise FeatureConfigurationError("instrument port must be a class")
        if not _instrumented_methods(port):
            raise FeatureConfigurationError(
                f"{port.__name__}: instrument requires public async methods or async __call__"
            )
        failures = tuple(expected)
        if any(
            not isinstance(cls, type) or not issubclass(cls, Exception)
            for cls in failures
        ):
            raise FeatureConfigurationError(
                "expected must contain Exception subclasses"
            )
        if not isinstance(component, str):
            raise FeatureConfigurationError("component must be a string")
        object.__setattr__(self, "port", port)
        object.__setattr__(self, "expected", failures)
        object.__setattr__(self, "component", component)


def instrument(
    port: type[Any],
    *,
    expected: Sequence[type[Exception]] = (),
    component: str = "",
) -> Instrumentation:
    """Declare instrumentation for a feature's port or async callable use case.

    Pass the result to ``Feature(instrumentation=[...])``. No OpenTelemetry
    import or Dishka decorator is created until composition enables telemetry.
    The component selects the Dishka binding; the default is the empty component.
    """
    return Instrumentation(port=port, expected=expected, component=component)


@dataclass(frozen=True, slots=True, kw_only=True)
class Telemetry:
    """Use application-owned providers, with FastAPI automatic exporters disabled."""

    exclude: Callable[[ASGIScope], bool] | None = None
    operation_spans: bool = True
    port_spans: bool = True
    port_metrics: bool = False
    problem_details: bool = True
    tracing: bool = True
    metrics: bool = True
    logs: bool = True
    tracer_provider: TracerProvider | None = None
    meter_provider: MeterProvider | None = None
    logger_provider: LoggerProvider | None = None

    def __post_init__(self) -> None:
        for name in (
            "operation_spans",
            "port_spans",
            "port_metrics",
            "problem_details",
            "tracing",
            "metrics",
            "logs",
        ):
            if not isinstance(getattr(self, name), bool):
                raise FeatureConfigurationError(f"{name} must be a bool")
        if self.exclude is not None and not callable(self.exclude):
            raise FeatureConfigurationError("exclude must be callable or None")

    def rpc_middleware(self) -> OpenTelemetry:
        """Create the WebSocket middleware explicitly, forwarding the same providers.

        Install ``fastapi-canon[websockets,otel]`` and pass this to ``RpcService``. No
        connection span is added, and RPC baggage propagation remains disabled.
        """
        from fastapi_canon.integrations.websockets.adapters.opentelemetry import (
            OpenTelemetry,
        )

        return OpenTelemetry(
            tracing=self.tracing,
            metrics=self.metrics,
            tracer_provider=self.tracer_provider,
            meter_provider=self.meter_provider,
        )

    def _prepare(self, app: FastAPI) -> None:
        from importlib.metadata import version

        installed = tuple(int(part) for part in version("fastapi").split(".")[:2])
        if installed < (0, 143) or not isinstance(
            getattr(app, "_telemetry", None), dict
        ):
            raise FeatureConfigurationError(
                "Canon telemetry requires FastAPI >=0.143; core still supports FastAPI >=0.115"
            )
        try:
            # lazy telemetry adapter factory
            from fastapi_canon.integrations.opentelemetry.native import (  # smelt: ignore[SMT104] -- factory cycle
                validate_app,
            )
        except ModuleNotFoundError as exc:
            if exc.name is None or not exc.name.startswith("opentelemetry"):
                raise
            raise ModuleNotFoundError(
                "Canon telemetry requires OpenTelemetry; install fastapi-canon[otel]",
                name=exc.name,
            ) from exc

        validate_app(app)


def _instrumentation_providers(
    registrations: tuple[tuple[str, Instrumentation], ...],
    options: Telemetry | None,
) -> tuple[Provider, ...]:
    seen: dict[tuple[str, type[Any]], str] = {}
    for feature, spec in registrations:
        key = (spec.component, spec.port)
        if key in seen:
            raise FeatureConfigurationError(
                f"port {spec.port.__name__} is instrumented more than once in "
                f"component {spec.component!r} by features {seen[key]!r} and {feature!r}"
            )
        seen[key] = feature
    if options is None or not registrations:
        return ()
    from fastapi_canon.integrations.opentelemetry.ports import _providers

    return _providers(registrations, options)


def _instrumented_methods(
    port: type[Any],
) -> tuple[tuple[str, Callable[..., Any]], ...]:
    return tuple(
        (name, method)
        for name, method in inspect.getmembers(port, inspect.iscoroutinefunction)
        if name == "__call__" or not name.startswith("_")
    )
