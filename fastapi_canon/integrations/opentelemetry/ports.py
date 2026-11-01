"""Port instrumentation loaded only by an enabled telemetry composition."""

from collections.abc import Callable
from contextlib import nullcontext
from contextvars import ContextVar
from functools import wraps
from time import perf_counter
from typing import Any

from dishka import Provider

try:
    from opentelemetry import metrics, trace
    from opentelemetry.trace import SpanKind, StatusCode
except ModuleNotFoundError as exc:
    raise ModuleNotFoundError(
        "Canon telemetry requires OpenTelemetry; install fastapi-canon[otel]",
        name=exc.name,
    ) from exc

from fastapi_canon.integrations.opentelemetry.declarations import (
    Instrumentation,
    Telemetry,
    _instrumented_methods,
)

excluded: ContextVar[bool] = ContextVar("canon.telemetry.excluded", default=False)


class _InstrumentedProxy:
    def __init__(self, instance: Any) -> None:
        self._instance = instance

    def __getattr__(self, name: str) -> Any:
        return getattr(self._instance, name)


class _Observer:
    def __init__(
        self, registrations: tuple[tuple[str, Instrumentation], ...], options: Telemetry
    ) -> None:
        self.tracer = trace.get_tracer(
            "fastapi_canon", tracer_provider=options.tracer_provider
        )
        self.histogram = (
            metrics.get_meter(
                "fastapi_canon", meter_provider=options.meter_provider
            ).create_histogram("canon.port.duration", unit="s")
            if options.port_metrics
            else None
        )
        self.spans = options.tracing and options.port_spans
        self.proxies = {
            spec: type(
                f"Instrumented{spec.port.__name__}",
                (_InstrumentedProxy,),
                {
                    name: self._wrapper(spec, feature, name, method)
                    for name, method in _instrumented_methods(spec.port)
                },
            )
            for feature, spec in registrations
        }

    def wrap(self, spec: Instrumentation, instance: Any) -> Any:
        if not self.spans and self.histogram is None:
            return instance
        return self.proxies[spec](instance)

    def _wrapper(
        self,
        spec: Instrumentation,
        feature: str,
        name: str,
        method: Callable[..., Any],
    ) -> Callable[..., Any]:
        registered: dict[str, str] = {
            "canon.feature": feature,
            "canon.port": spec.port.__name__,
            "code.function.name": f"{spec.port.__name__}.{name}",
        }

        @wraps(method)
        async def call(proxy: _InstrumentedProxy, *args: Any, **kwargs: Any) -> Any:
            target = getattr(proxy._instance, name)
            if excluded.get():
                return await target(*args, **kwargs)
            attributes = {**registered, "canon.adapter": type(proxy._instance).__name__}
            started = perf_counter()
            outcome = dict(registered)
            with (
                self.tracer.start_as_current_span(
                    registered["code.function.name"],
                    kind=SpanKind.INTERNAL,
                    attributes=attributes,
                    record_exception=False,
                    set_status_on_exception=False,
                )
                if self.spans
                else nullcontext(None)
            ) as span:
                try:
                    return await target(*args, **kwargs)
                except Exception as error:
                    error_type = type(error).__name__
                    expected = isinstance(error, spec.expected)
                    if not expected:
                        outcome["error.type"] = error_type
                    if span is not None:
                        # Exception messages/stack traces can contain arguments.
                        span.add_event(
                            "canon.port.expected_exception"
                            if expected
                            else "exception",
                            {"exception.type": error_type},
                        )
                        if not expected:
                            span.set_attribute("error.type", error_type)
                            span.set_status(StatusCode.ERROR)
                    raise
                finally:
                    if self.histogram is not None:
                        self.histogram.record(perf_counter() - started, outcome)

        return call


def _providers(
    registrations: tuple[tuple[str, Instrumentation], ...], options: Telemetry
) -> tuple[Provider, ...]:
    observer = _Observer(registrations, options)
    return tuple(_provider(spec, observer) for _, spec in registrations)


def _provider(spec: Instrumentation, observer: _Observer) -> Provider:
    def wrap(instance: Any) -> Any:
        return observer.wrap(spec, instance)

    wrap.__annotations__ = {"instance": spec.port, "return": spec.port}
    provider = Provider(component=spec.component)
    provider.decorate(wrap, provides=spec.port)
    return provider
