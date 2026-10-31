import asyncio
from collections.abc import AsyncGenerator, Callable, Generator, Mapping
from contextlib import asynccontextmanager, contextmanager, nullcontext
from contextvars import ContextVar

try:
    from opentelemetry import context, propagate, trace
    from opentelemetry import metrics as otel_metrics
    from opentelemetry.baggage.propagation import W3CBaggagePropagator
    from opentelemetry.metrics import MeterProvider
    from opentelemetry.trace import (
        Link,
        Span,
        SpanContext,
        SpanKind,
        StatusCode,
        TracerProvider,
    )
    from opentelemetry.trace.propagation.tracecontext import (
        TraceContextTextMapPropagator,
    )
except ImportError as error:
    raise ModuleNotFoundError(
        "WebSocket OpenTelemetry middleware requires the 'otel' extra; "
        "install fastapi-canon[otel]",
        name="opentelemetry",
    ) from error

from fastapi_canon.integrations.websockets.connection import (
    RpcConnection,
    RpcConnectionClose,
)
from fastapi_canon.integrations.websockets.middleware import (
    RpcConnectionScope,
    RpcMiddleware,
    RpcRequestScope,
    RpcStreamScope,
    _current_serving,
)
from fastapi_canon.integrations.websockets.request_metadata import (
    validate_request_metadata,
)

type RpcTelemetryExclude = Callable[[RpcConnectionScope | RpcRequestScope], bool]
type _Attributes = dict[str, str | bool]

_REGULAR_CLOSES = (
    None,
    RpcConnectionClose.NORMAL,
    RpcConnectionClose.SHUTDOWN,
    RpcConnectionClose.SERVICE_RESTART,
)


def trace_metadata(*, include_baggage: bool = False) -> dict[str, str]:
    """Return the current W3C context as an RPC request metadata carrier."""
    carrier: dict[str, str] = {}
    TraceContextTextMapPropagator().inject(carrier)
    if include_baggage:
        W3CBaggagePropagator().inject(carrier)
    return validate_request_metadata(carrier)


def _remote_context(
    carrier: Mapping[str, str], include_baggage: bool
) -> context.Context:
    parent = TraceContextTextMapPropagator().extract(
        dict(carrier), context=context.Context()
    )
    if include_baggage:
        parent = W3CBaggagePropagator().extract(dict(carrier), context=parent)
    return parent


@contextmanager
def _attached_context(parent: context.Context | None) -> Generator[None, None, None]:
    token = None if parent is None else context.attach(parent)
    try:
        yield
    finally:
        if token is not None:
            context.detach(token)


class OpenTelemetry(RpcMiddleware):
    """Use application OTEL providers to trace and measure RPC and stream operations."""

    def __init__(
        self,
        *,
        tracing: bool = True,
        metrics: bool = True,
        connection_spans: bool = False,
        propagate_baggage: bool = False,
        record_exceptions: bool = False,
        tracer_provider: TracerProvider | None = None,
        meter_provider: MeterProvider | None = None,
        exclude: RpcTelemetryExclude | None = None,
    ) -> None:
        self._tracing = tracing
        self._metrics = metrics
        self._connection_spans = connection_spans
        self._propagate_baggage = propagate_baggage
        self._connection_span: ContextVar[SpanContext | None] = ContextVar(
            "rpckit.otel.connection_span", default=None
        )
        self._record_exceptions = record_exceptions
        self._exclude = exclude
        self._excluded: ContextVar[bool] = ContextVar(
            "rpckit.otel.exclude", default=False
        )
        self._tracer = trace.get_tracer("rpckit", tracer_provider=tracer_provider)
        meter = otel_metrics.get_meter("rpckit", meter_provider=meter_provider)
        self._requests = meter.create_counter("rpckit.rpc.calls", unit="{call}")
        self._active_requests = meter.create_up_down_counter(
            "rpckit.rpc.active", unit="{call}"
        )
        self._request_duration = meter.create_histogram("rpckit.rpc.duration", unit="s")
        self._connections = meter.create_counter(
            "rpckit.connections", unit="{connection}"
        )
        self._active_connections = meter.create_up_down_counter(
            "rpckit.connections.active", unit="{connection}"
        )
        self._connection_duration = meter.create_histogram(
            "rpckit.connections.duration", unit="s"
        )
        self._streams = meter.create_counter("rpckit.streams", unit="{stream}")
        self._active_streams = meter.create_up_down_counter(
            "rpckit.streams.active", unit="{stream}"
        )
        self._stream_duration = meter.create_histogram(
            "rpckit.streams.duration", unit="s"
        )
        self._frames = meter.create_counter("rpckit.stream.frames", unit="{frame}")
        self._bytes = meter.create_counter("rpckit.stream.bytes", unit="By")
        self._notifications = meter.create_counter(
            "rpckit.notifications", unit="{notification}"
        )
        self._slow_consumers = meter.create_counter(
            "rpckit.slow_consumers", unit="{connection}"
        )

    @contextmanager
    def _span(
        self,
        name: str,
        kind: SpanKind,
        attributes: _Attributes,
        *,
        parent: context.Context | None = None,
        links: tuple[Link, ...] = (),
    ) -> Generator[Span | None, None, None]:
        if not self._tracing:
            with _attached_context(parent):
                yield None
            return
        with (
            _attached_context(parent),
            self._tracer.start_as_current_span(
                name,
                kind=kind,
                attributes=attributes,
                context=parent,
                links=links,
                record_exception=False,
                set_status_on_exception=False,
            ) as span,
        ):
            yield span

    def _finish(
        self,
        span: Span | None,
        error: BaseException | None,
        cancelled: bool,
        error_type: str | None,
    ) -> None:
        if span is None:
            return
        if cancelled:
            span.set_attribute("rpckit.cancelled", True)
        if error_type is not None or (
            error is not None and not isinstance(error, asyncio.CancelledError)
        ):
            span.set_status(StatusCode.ERROR)
            span.set_attribute("error.type", error_type or type(error).__qualname__)
            if self._record_exceptions and error is not None:
                span.record_exception(error)

    @asynccontextmanager
    async def connection(self, scope: RpcConnectionScope) -> AsyncGenerator[None, None]:
        excluded = self._exclude is not None and self._exclude(scope)
        exclusion = self._excluded.set(excluded)
        attached = None
        try:
            if excluded:
                yield
                return
            parent = context.get_current()
            if not trace.get_current_span().get_span_context().is_valid:
                headers = {k.lower(): v for k, v in scope.handshake.headers.items()}
                if not self._propagate_baggage:
                    headers.pop("baggage", None)
                parent = propagate.extract(headers)
            attached = context.attach(parent)
            attributes = _endpoint_attributes(scope)
            manager = (
                self._span(f"WS {scope.endpoint.path}", SpanKind.SERVER, attributes)
                if self._connection_spans
                else nullcontext(None)
            )
            with manager as span:
                connection_token = self._connection_span.set(
                    None if span is None else span.get_span_context()
                )
                try:
                    yield
                finally:
                    self._connection_span.reset(connection_token)
                    outcome = scope.outcome
                    if outcome is not None:
                        error_type = (
                            outcome.rejection.value
                            if outcome.rejection is not None
                            else _close_error(outcome.close_code)
                        )
                        self._finish(span, outcome.error, outcome.cancelled, error_type)
                        result = {
                            **attributes,
                            "rpckit.outcome": (
                                outcome.rejection.value
                                if outcome.rejection is not None
                                else "accepted"
                                if outcome.accepted
                                else "failed"
                            ),
                        }
                        if self._metrics:
                            if not outcome.accepted:
                                self._connections.add(1, result)
                            if outcome.accepted:
                                self._active_connections.add(-1, attributes)
                                self._connection_duration.record(
                                    outcome.accepted_duration or 0.0,
                                    _close_attributes(result, outcome.close_code),
                                )
        finally:
            if attached is not None:
                context.detach(attached)
            self._excluded.reset(exclusion)

    @asynccontextmanager
    async def request(self, scope: RpcRequestScope) -> AsyncGenerator[None, None]:
        if self._excluded.get() or (self._exclude is not None and self._exclude(scope)):
            yield
            return
        attributes: _Attributes = {
            "rpc.system.name": "jsonrpc",
            "jsonrpc.protocol.version": "2.0",
            "rpckit.notification": scope.request.notification,
        }
        name = "RPC"
        if scope.method is not None:
            name = scope.method.name
            attributes["rpc.method"] = name
        if scope.endpoint is not None:
            attributes.update(_endpoint_attributes(scope))
        if self._metrics:
            self._active_requests.add(1, attributes)
        parent = None
        links: tuple[Link, ...] = ()
        if scope.metadata is not None:
            parent = _remote_context(scope.metadata, self._propagate_baggage)
            connection_span = self._connection_span.get()
            if connection_span is not None and connection_span.is_valid:
                links = (Link(connection_span),)
        with self._span(
            name, SpanKind.SERVER, attributes, parent=parent, links=links
        ) as span:
            try:
                yield
            finally:
                if self._metrics:
                    self._active_requests.add(-1, attributes)
                outcome = scope.outcome
                if outcome is not None:
                    self._finish(
                        span, outcome.error, outcome.cancelled, outcome.error_code
                    )
                    if span is not None and outcome.error_code is not None:
                        span.set_attribute(
                            "rpc.response.status_code", outcome.error_code
                        )
                    result = {
                        **attributes,
                        "rpckit.outcome": "cancelled"
                        if outcome.cancelled
                        else "error"
                        if outcome.error is not None or outcome.error_code is not None
                        else "success",
                    }
                    if self._metrics:
                        self._requests.add(1, result)
                        self._request_duration.record(outcome.duration, result)

    @asynccontextmanager
    async def stream(self, scope: RpcStreamScope) -> AsyncGenerator[None, None]:
        if self._excluded.get():
            yield
            return
        attributes = _stream_attributes(scope)
        if self._metrics:
            self._active_streams.add(1, attributes)
        with self._span(
            scope.endpoint.stream.name, SpanKind.INTERNAL, attributes
        ) as span:
            try:
                yield
            finally:
                if self._metrics:
                    self._active_streams.add(-1, attributes)
                outcome = scope.outcome
                if outcome is not None:
                    self._finish(
                        span,
                        outcome.error,
                        outcome.cancelled,
                        _close_error(outcome.close_code),
                    )
                    if self._metrics:
                        result = _close_attributes(attributes, outcome.close_code)
                        self._streams.add(1, result)
                        self._stream_duration.record(outcome.duration, result)

    async def connection_opened(self, connection: RpcConnection) -> None:
        state = _current_serving()
        if self._metrics and not self._excluded.get() and state is not None:
            attributes = _endpoint_attributes(state.scope)
            self._active_connections.add(1, attributes)
            self._connections.add(1, {**attributes, "rpckit.outcome": "accepted"})

    async def _frame(self, size: int, direction: str) -> None:
        state = _current_serving()
        if self._metrics and not self._excluded.get() and state is not None:
            attributes = {
                **_endpoint_attributes(state.scope),
                "rpckit.direction": direction,
            }
            self._frames.add(1, attributes)
            self._bytes.add(size, attributes)

    async def stream_frame_sent(self, connection: RpcConnection, size: int) -> None:
        await self._frame(size, "outbound")

    async def stream_frame_received(self, connection: RpcConnection, size: int) -> None:
        await self._frame(size, "inbound")

    async def notification_sent(self, name: str, size: int) -> None:
        if self._metrics and not self._excluded.get():
            self._notifications.add(1, {"rpc.method": name})

    async def slow_consumer_closed(self, connection: RpcConnection) -> None:
        state = _current_serving()
        if self._metrics and not self._excluded.get() and state is not None:
            self._slow_consumers.add(1, _endpoint_attributes(state.scope))


def _endpoint_attributes(
    scope: RpcConnectionScope | RpcRequestScope | RpcStreamScope,
) -> _Attributes:
    endpoint = scope.endpoint
    if endpoint is None:
        return {}
    return {
        "rpckit.endpoint": endpoint.name,
        "rpckit.route": endpoint.path,
    }


def _stream_attributes(scope: RpcStreamScope) -> _Attributes:
    return {
        **_endpoint_attributes(scope),
        "rpckit.stream": scope.endpoint.stream.name,
        "rpckit.stream.direction": scope.endpoint.stream.direction.value,
    }


def _close_error(close: RpcConnectionClose | None) -> str | None:
    return None if close in _REGULAR_CLOSES else close.value


def _close_attributes(
    attributes: _Attributes, close: RpcConnectionClose | None
) -> _Attributes:
    return {
        **attributes,
        "rpckit.close": "unknown" if close is None else close.value,
    }
