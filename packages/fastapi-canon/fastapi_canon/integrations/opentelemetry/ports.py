"""Port instrumentation loaded only by an enabled telemetry composition."""

import inspect
from collections.abc import Callable
from contextlib import nullcontext
from contextvars import ContextVar
from functools import wraps
from time import perf_counter
from typing import Any

try:
    from opentelemetry import metrics, trace
    from opentelemetry.trace import SpanKind, StatusCode
except ModuleNotFoundError as exc:
    raise ModuleNotFoundError(
        "Canon telemetry requires OpenTelemetry; install fastapi-canon[otel]",
        name=exc.name,
    ) from exc

from fastapi_canon.integrations.opentelemetry.declarations import (
    Telemetry,
    _Observation,
    _PortObserver,
)

excluded: ContextVar[bool] = ContextVar("canon.telemetry.excluded", default=False)


class _ObservedProxy:
    def __init__(self, instance: Any) -> None:
        self._instance = instance

    def __getattr__(self, name: str) -> Any:
        return getattr(self._instance, name)


class _Observer(_PortObserver):
    def __init__(
        self, observations: dict[tuple[_Observation, str], str], options: Telemetry
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
            (spec, component): type(
                f"Observed{spec.port.__name__}",
                (_ObservedProxy,),
                {
                    name: self._wrapper(spec, feature, name, method)
                    for name, method in inspect.getmembers(
                        spec.port, inspect.iscoroutinefunction
                    )
                    if not name.startswith("_")
                },
            )
            for (spec, component), feature in observations.items()
        }

    def wrap(self, spec: _Observation, component: str, instance: Any) -> Any:
        if not self.spans and self.histogram is None:
            return instance
        return self.proxies[spec, component](instance)

    def _wrapper(
        self,
        spec: _Observation,
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
        async def call(proxy: _ObservedProxy, *args: Any, **kwargs: Any) -> Any:
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
