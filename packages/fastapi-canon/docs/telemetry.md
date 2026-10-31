# Optional OpenTelemetry integration

Canon adds port spans to FastAPI's native HTTP telemetry. Endpoints, application
services and domain classes need no telemetry annotations or imports.

Install `fastapi-canon[otel]`. This extra requires FastAPI >=0.143 and the
OpenTelemetry API; the SDK and exporters remain application dependencies. Core
keeps FastAPI >=0.115 and can run without OpenTelemetry on older FastAPI versions.
Recent FastAPI versions themselves depend on the OpenTelemetry API.

## Developer-facing API

Register instrumentation beside the Dishka binding it decorates:

```python
from dishka import Provider, Scope, provide
from fastapi import FastAPI
from fastapi_canon import Composition, Feature
from fastapi_canon.integrations.opentelemetry import Telemetry, observe


class TaskProvider(Provider):
    repository = provide(
        SqlTaskRepository,
        provides=TaskRepository,
        scope=Scope.REQUEST,
    )
    service = provide(TaskService, scope=Scope.REQUEST)

    repository_telemetry = observe(TaskRepository)
    service_telemetry = observe(TaskService, expected=(TaskNotFound,))


tasks = Feature(name="tasks", routers=(router,), providers=(TaskProvider,))
app = Composition(
    tasks,
    telemetry=Telemetry(
        tracer_provider=tracer_provider,
        exclude=lambda scope: scope["path"] in {"/health", "/ready"},
    ),
).apply(FastAPI())
```

`observe(Port)` returns a Dishka decorator declaration for a Provider class.
Existing `@provide` factory methods work too. The declaration can remain in the
provider unconditionally: without `Composition(telemetry=...)`, it returns the
original instance and imports no Canon OpenTelemetry runtime. `Feature` needs
no additional metadata. Use the decorator inside a Canon composition, which
supplies its observer dependency.

Every public coroutine method declared on the port, including inherited methods,
gets an `INTERNAL` span. Synchronous methods, private methods and async generators
are passed through. At least one public coroutine method is required. There are
no method-name strings, parameter mappings, or implicit argument capture. For
custom attributes, use an explicit application-owned adapter and ordinary
OpenTelemetry calls.

Dishka still owns scope, caching and finalization of the original dependency.
The integration caches a proxy class and method wrappers per registration and
composition, then creates one proxy per dependency resolution. It forwards method
calls and public attribute reads; it is not a general replacement for object
identity, concrete-type checks, attribute mutation, or Python special methods.
Register a port only once per Dishka component. Existing named components are
supported; the observer dependency lives in the default component.

## Configuration and ownership

`Telemetry` is immutable and keyword-only:

| Option | Default | Meaning |
| --- | --- | --- |
| `tracer_provider`, `meter_provider`, `logger_provider` | `None` | Use the current global provider; explicit providers are local to this application |
| `tracing`, `metrics`, `logs` | `True` | Enable FastAPI's native signals; `tracing` also gates port spans |
| `operation_spans` | `True` | FastAPI dependencies, endpoint, serialization and background-task spans |
| `port_spans` | `True` | Instrument observed coroutine methods |
| `port_metrics` | `False` | Record `canon.port.duration` in seconds, independently of port spans |
| `problem_details` | `True` | Enrich the native HTTP server span with handled Problem Details outcomes |
| `exclude` | `None` | ASGI scope predicate; suppress native telemetry and port instrumentation for that request/connection |

Apply composition before startup. Canon forces FastAPI's `auto_configure=False`,
including when the environment requests automatic exporters. It never creates
exporters, sets global providers, or flushes/shuts down application providers.
The application may install global providers in its lifespan before requests
start; both FastAPI and the port tracer resolve them lazily. Explicit providers
should be created before composition and shut down by an application lifespan.

FastAPI currently accepts telemetry configuration only in its constructor.
Canon bridges that boundary by updating the existing `_telemetry` dictionary,
guarded by a FastAPI >=0.143 check and integration tests. When problem enrichment
is enabled, a local, non-owning tracer-provider adapter preserves problem codes
when FastAPI finalizes a 5xx span. It does not replace the global provider.

## Span and error contracts

Port spans use `{Port}.{method}`, such as `TaskRepository.get_by_id`, with:

- `code.function.name`: the same port/method name;
- `canon.feature`: the feature contributing the provider;
- `canon.port`: the port class name;
- `canon.adapter`: the resolved implementation class name.

An unexpected exception is re-raised, adds an `exception` event containing only
`exception.type`, and sets `error.type` to the exception class name and status to
`ERROR`. Exception messages, stack traces, arguments, return values, payloads,
headers, and SQL parameters are not captured by the port wrapper.

`observe(Port, expected=(TaskNotFound,))` instead records a
`canon.port.expected_exception` event with `exception.type`, leaves span status
unset, and omits `error.type`. Subclasses match too. This is an application
classification, not a universal OpenTelemetry rule. Cancellation propagates
without an error event/status. These settings apply to that port only; configure
other observed ports independently when the same exception crosses them.

The optional histogram uses only `canon.feature`, `canon.port`,
`code.function.name`, and, for unexpected failures, `error.type`. Argument values
and adapter names never become metric labels.

For handled Problem Details, the native server span gets `canon.problem.code`
from the stable response code. Only 5xx also set `error.type` to that code and
status to `ERROR`. A handled 4xx leaves server span status unset and does not add
`error.type`, following the [HTTP conventions](https://opentelemetry.io/docs/specs/semconv/http/http-spans/).
This includes domain errors, validation, HTTP exceptions, rendering failures,
and unhandled exceptions normalized by Canon's error handlers.

FastAPI's native `fastapi.endpoint` span can still mark a propagated domain
exception as `ERROR`, even if Canon later turns it into an expected 404. Canon
does not rewrite FastAPI's operation spans. Use the HTTP server status and port
classification when defining alerts; set `operation_spans=False` if only server
and application-boundary spans are wanted. See also the
[general error conventions](https://opentelemetry.io/docs/specs/semconv/general/recording-errors/).

## Third-party instrumentation and RPC

Library instrumentation belongs to the application provider that owns the engine
or client. The [reference demo](../../../examples/reference-service/README.md#local-telemetry-demo)
uses an instrumented HTTPX2 client to propagate W3C context to a second service,
and SQLAlchemy instrumentation to add database spans below repository spans.
These packages are application extras, not dependencies of Canon's `otel` extra.

HTTPX2 supports `HTTPX2ClientInstrumentor.instrument_client(client, ...)` and
`uninstrument_client(client)`. SQLAlchemy's documented
`SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine, ...)` also wraps
engine factories process-wide, and `uninstrument()` removes its instrumentation
globally. The demo owns that setup in its dedicated process; do not interpret
the engine argument as a guarantee of isolation between applications in one
process. SQLCommenter stays disabled. Bound SQL parameters are not captured,
but statement text may contain literals if an application interpolates them.

RPC is explicit because Canon does not own `RpcService` instances:

```python
telemetry = Telemetry(tracer_provider=tracer_provider, meter_provider=meter_provider)
rpc = RpcService(middleware=(telemetry.rpc_middleware(),))
app = Composition(tasks, telemetry=telemetry).apply(FastAPI())
```

Select `fastapi-canon[websockets,otel]` to enable the bundled middleware. The
helper forwards providers and the `tracing`/`metrics` switches. It keeps the
default `connection_spans=False`, so operations inherit FastAPI's active
WebSocket operation/connection context, and leaves baggage propagation disabled.
ASGI exclusions do not configure per-operation exclusions; use
`fastapi_canon.integrations.websockets.adapters.opentelemetry.OpenTelemetry` directly
for those controls.

FastAPI disables its native instrumentation when contrib ASGI instrumentation
already owns the middleware stack. Canon's native server-span enrichment then
does not apply. Prefer one HTTP instrumentation owner. SSE receives FastAPI's
normal HTTP tracing; dedicated SSE event instrumentation is outside this API.

## Testing, logs and sampling

Install `opentelemetry-sdk` in the application's test dependencies:

```python
from fastapi_canon.testing.telemetry import SpanCapture

with SpanCapture() as spans:
    app = create_app(telemetry=Telemetry(tracer_provider=spans.provider))
    with TestClient(app) as client:
        client.post("/tasks/123/completion")
    spans.assert_tree(
        "POST /tasks/{task_id}/completion",
        [("fastapi.endpoint", [("TaskService.complete", ["TaskRepository.save"])])],
    )
```

`SpanCapture.one(name)` requires exactly one finished span. `assert_tree` verifies
direct nesting and sibling order while ignoring unlisted children such as SQL
spans. `clear()` separates requests; the context manager shuts down only its own
provider and never changes global state.

Applications own log correlation via their LoggerProvider/logging handler. Canon
adds no telemetry log pipeline. FastAPI's exception logs can include messages and
stack traces; set `logs=False` or apply application log redaction when needed.
`ParentBased(TraceIdRatioBased(ratio))` lets downstream services follow the
caller's sampling decision. Tail sampling belongs in the collector.
