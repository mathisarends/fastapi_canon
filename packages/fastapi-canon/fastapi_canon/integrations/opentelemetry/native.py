"""The version-guarded FastAPI configuration bridge and non-owning span adapter."""

from collections.abc import Mapping
from importlib.metadata import version
from typing import Any

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.trace import Span, SpanContext, SpanKind, Status, StatusCode
from starlette.types import ASGIApp, Receive, Scope, Send

from fastapi_canon.feature import FeatureConfigurationError
from fastapi_canon.integrations.opentelemetry.declarations import Telemetry
from fastapi_canon.integrations.opentelemetry.ports import excluded


def validate_app(app: FastAPI) -> None:
    installed = tuple(int(part) for part in version("fastapi").split(".")[:2])
    if installed < (0, 143) or not isinstance(getattr(app, "_telemetry", None), dict):
        raise FeatureConfigurationError(
            "Canon telemetry requires FastAPI >=0.143; core still supports FastAPI >=0.115"
        )
    if app.middleware_stack is not None:
        raise FeatureConfigurationError("install telemetry before the app starts")


def install(app: FastAPI, options: Telemetry) -> None:
    # FastAPI currently exposes telemetry only in its constructor. Updating the
    # existing dictionary keeps its NativeTelemetry/lifespan configuration shared.
    config = app._telemetry
    config.update(
        {
            "auto_configure": False,
            "tracing": options.tracing,
            "metrics": options.metrics,
            "logs": options.logs,
            "operation_spans": options.operation_spans,
            "exclude": lambda scope: _skip(scope, options),
            "tracer_provider": (
                _ProblemProvider(options.tracer_provider)
                if options.problem_details
                else options.tracer_provider
            ),
            "meter_provider": options.meter_provider,
            "logger_provider": options.logger_provider,
        }
    )
    app.add_middleware(_RequestContext, options=options)
    if options.problem_details:
        app.state._canon_problem_telemetry = _problem


def _problem(scope: Scope, code: str, status: int) -> None:
    span = scope.get("_canon_server_span")
    if isinstance(span, _ProblemSpan):
        span.set_attribute("canon.problem.code", code)
        if status >= 500:
            span.problem = code
            span.set_attribute("error.type", code)
            span.set_status(StatusCode.ERROR)


def _skip(scope: Scope, options: Telemetry) -> bool:
    key = f"_canon_telemetry_excluded_{id(options)}"
    if key not in scope:
        scope[key] = options.exclude is not None and options.exclude(scope)
    return bool(scope[key])


class _RequestContext:
    def __init__(self, app: ASGIApp, options: Telemetry) -> None:
        self.app = app
        self.options = options

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        skip = _skip(scope, self.options)
        exclusion = excluded.set(skip)
        scope["_canon_server_span"] = None if skip else trace.get_current_span()
        try:
            await self.app(scope, receive, send)
        finally:
            excluded.reset(exclusion)


class _ProblemProvider(trace.TracerProvider):
    """Delegate to an application/global provider without owning its lifecycle."""

    def __init__(self, provider: trace.TracerProvider | None) -> None:
        self.provider = provider

    def get_tracer(
        self,
        instrumenting_module_name: str,
        instrumenting_library_version: str | None = None,
        schema_url: str | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> trace.Tracer:
        return _ProblemTracer(
            trace.get_tracer(
                instrumenting_module_name,
                instrumenting_library_version,
                tracer_provider=self.provider,
                schema_url=schema_url,
                attributes=attributes,
            )
        )


class _ProblemTracer(trace.Tracer):
    def __init__(self, tracer: trace.Tracer) -> None:
        self.tracer = tracer

    def start_span(self, *args: Any, **kwargs: Any) -> Span:
        span = self.tracer.start_span(*args, **kwargs)
        kind = kwargs.get("kind", args[2] if len(args) > 2 else SpanKind.INTERNAL)
        return _ProblemSpan(span) if kind == SpanKind.SERVER else span

    def start_as_current_span(self, *args: Any, **kwargs: Any) -> Any:
        return self.tracer.start_as_current_span(*args, **kwargs)


class _ProblemSpan(Span):
    """Preserve the problem slug when FastAPI finalizes a 5xx server span."""

    def __init__(self, span: Span) -> None:
        self.span = span
        self.problem: str | None = None

    def end(self, end_time: int | None = None) -> None:
        if self.problem is not None:
            self.span.set_attribute("error.type", self.problem)
        self.span.end(end_time)

    def get_span_context(self) -> SpanContext:
        return self.span.get_span_context()

    def is_recording(self) -> bool:
        return self.span.is_recording()

    def set_attribute(self, key: str, value: Any) -> None:
        self.span.set_attribute(key, value)

    def set_attributes(self, attributes: Mapping[str, Any]) -> None:
        self.span.set_attributes(attributes)

    def add_event(self, *args: Any, **kwargs: Any) -> None:
        self.span.add_event(*args, **kwargs)

    def add_link(self, *args: Any, **kwargs: Any) -> None:
        self.span.add_link(*args, **kwargs)

    def update_name(self, name: str) -> None:
        self.span.update_name(name)

    def set_status(
        self, status: Status | StatusCode, description: str | None = None
    ) -> None:
        self.span.set_status(status, description)

    def record_exception(self, *args: Any, **kwargs: Any) -> None:
        self.span.record_exception(*args, **kwargs)
