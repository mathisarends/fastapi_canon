"""Optional telemetry declarations; importing this module needs no OTEL packages."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from dishka import FromComponent, Provider, Scope, decorate
from dishka.dependency_source import CompositeDependencySource
from starlette.types import Scope as ASGIScope

from fastapi_canon.feature import FeatureConfigurationError

if TYPE_CHECKING:
    from fastapi import FastAPI
    from opentelemetry._logs import LoggerProvider
    from opentelemetry.metrics import MeterProvider
    from opentelemetry.trace import TracerProvider

    from fastapi_canon.integrations.websockets.adapters.opentelemetry import (
        OpenTelemetry,
    )

__all__ = ["Telemetry", "observe"]


@dataclass(frozen=True, slots=True, eq=False)
class _Observation:
    port: type[Any]
    expected: tuple[type[Exception], ...]


class _PortObserver:
    """No-op implementation for compositions without telemetry."""

    def wrap(self, spec: _Observation, component: str, instance: Any) -> Any:
        return instance


def observe(
    port: type[Any],
    *,
    expected: Sequence[type[Exception]] = (),
) -> CompositeDependencySource:
    """Declare a Dishka decorator on a Provider for all public async methods.

    No OpenTelemetry import or argument capture happens here. Without a
    Composition telemetry option, the decorator returns the original instance.
    Dishka owns the decorated dependency's scope, caching, and finalization.
    """
    if not isinstance(port, type):
        raise FeatureConfigurationError("observe port must be a class")
    if not any(
        not name.startswith("_") and inspect.iscoroutinefunction(method)
        for name, method in inspect.getmembers(port)
    ):
        raise FeatureConfigurationError(
            f"{port.__name__}: observe requires public async methods"
        )
    failures = tuple(expected)
    if any(
        not isinstance(cls, type) or not issubclass(cls, Exception) for cls in failures
    ):
        raise FeatureConfigurationError("expected must contain Exception subclasses")
    spec = _Observation(port, failures)

    def wrap(self: Provider, instance: Any, observer: _PortObserver) -> Any:
        return observer.wrap(spec, self.component, instance)

    wrap.__annotations__ = {
        "instance": port,
        "observer": Annotated[_PortObserver, FromComponent("")],
        "return": port,
    }
    wrap._canon_observation = spec  # type: ignore[attr-defined]
    return decorate(wrap, provides=port)


def _observer_provider(
    contributions: tuple[tuple[str, Provider], ...],
    options: Telemetry | None,
) -> Provider | None:
    observations: dict[tuple[_Observation, str], str] = {}
    seen: set[tuple[str, type[Any]]] = set()
    for feature, provider in contributions:
        for decorator in provider.decorators:
            source = decorator.factory.source
            source = getattr(source, "__func__", source)
            spec = getattr(source, "_canon_observation", None)
            if not isinstance(spec, _Observation):
                continue
            key = (provider.component, spec.port)
            if key in seen:
                raise FeatureConfigurationError(
                    f"port {spec.port.__name__} is observed more than once in component {provider.component!r}"
                )
            seen.add(key)
            observations[spec, provider.component] = feature
    if not observations:
        return None
    observer = _PortObserver()
    if options is not None:
        from fastapi_canon.integrations.opentelemetry.ports import _Observer

        observer = _Observer(observations, options)
    provider = Provider()
    provider.provide(lambda: observer, provides=_PortObserver, scope=Scope.APP)
    return provider


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
            from fastapi_canon.integrations.opentelemetry.native import validate_app
        except ModuleNotFoundError as exc:
            if exc.name is None or not exc.name.startswith("opentelemetry"):
                raise
            raise ModuleNotFoundError(
                "Canon telemetry requires OpenTelemetry; install fastapi-canon[otel]",
                name=exc.name,
            ) from exc

        validate_app(app)
