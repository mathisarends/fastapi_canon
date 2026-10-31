# Middleware and OpenTelemetry

Instrument RPC calls, notifications, connections, and binary streams with
`middleware=`. Lifecycle hooks run around execution, so tracing context reaches
application handlers and their child tasks. Middleware is the single server
and generated-client instrumentation API.

## Enable OpenTelemetry

```bash
uv add "fastapi-canon[websockets,fastapi,otel]"
```

The extra installs the OpenTelemetry API. Configure the SDK and exporters in
your application, or pass providers from your existing telemetry setup. fastapi_canon.integrations.websockets
does not create exporters, replace global providers, or shut down application
providers. Without a configured SDK, the OTEL API uses its normal no-op behavior.
[OpenTelemetry Python instrumentation](https://opentelemetry.io/docs/languages/python/instrumentation/)

```python
from collections.abc import AsyncIterator

from fastapi import FastAPI

from fastapi_canon.integrations.websockets import RpcChannel, RpcService
from fastapi_canon.integrations.websockets import create_router
from fastapi_canon.integrations.websockets.adapters.opentelemetry import OpenTelemetry

media = RpcChannel("media")


@media.stream(content_type="application/octet-stream")
async def frames() -> AsyncIterator[bytes]:
    yield b"frame"


rpc = RpcService(middleware=[OpenTelemetry()])
rpc.stream("/media/frames", frames)

web = FastAPI()
web.include_router(create_router(rpc))
```

Configure the integration at one location. For endpoints mounted through
FastAPI dependencies, it can live on `RpcRoutes` instead:

```python
routes = RpcRoutes(
    router,
    context=open_session,
    middleware=[OpenTelemetry()],
)
routes.mount(session_media)
```

Here the router, context function, and stream endpoint belong to the application.
`middleware=` is also accepted by `create_router()`, `serve_websocket()`,
endpoint `serve()`, service `socket()` and `stream()`, and channel/endpoint
`create_server()`.

## Configure signals and providers

```python
telemetry = OpenTelemetry(
    tracing=True,
    metrics=True,
    connection_spans=False,
    propagate_baggage=False,
    record_exceptions=False,
    tracer_provider=None,
    meter_provider=None,
    exclude=None,
)
```

Provider arguments accept the OpenTelemetry API provider interfaces. Omitted
providers use the global providers. `tracing` and `metrics` operate independently.
Each RPC call, subscription control call, or incoming notification gets a
`SERVER` span. Each accepted binary stream gets an `INTERNAL` span covering
dependency resolution, execution, and cleanup. Application spans started in
handlers inherit the active operation span.

At connection time, an existing valid trace context, including FastAPI's
connection context, takes precedence. Otherwise the configured OTEL propagator
extracts context from
case-insensitive handshake headers. `connection_spans=True` adds a connection
span; the default avoids adding another connection span to FastAPI telemetry.

`exclude` receives `RpcConnectionScope` or `RpcRequestScope`. Return `True` to
skip that operation. Excluding a connection also excludes its operations and
frame metrics. Parallel connections have independent exclusion state.

Payloads, header values, concrete path variables, query strings, request IDs,
and close reasons are not captured automatically. Span names and metric labels
use registered operations, endpoint names, and path templates. Unknown methods
receive the span name `RPC` without a raw method attribute. Error categories
and status remain available with exception events disabled; explicitly enable
`record_exceptions` to include exception messages and stack traces.

## Metrics

These are fastapi_canon.integrations.websockets-specific instrument names. Durations are measured in seconds.

| Instrument | Measurement |
| --- | --- |
| `fastapi_canon.integrations.websockets.rpc.calls` | Completed calls, including error and cancellation outcomes |
| `fastapi_canon.integrations.websockets.rpc.active` | Calls executing now |
| `fastapi_canon.integrations.websockets.rpc.duration` | Call duration, excluding the concurrency queue and socket delivery |
| `fastapi_canon.integrations.websockets.connections` | Accepted connections and rejected or failed handshakes |
| `fastapi_canon.integrations.websockets.connections.active` | Accepted connections currently open |
| `fastapi_canon.integrations.websockets.connections.duration` | Accepted connection lifetime |
| `fastapi_canon.integrations.websockets.streams` | Completed binary stream operations by close category |
| `fastapi_canon.integrations.websockets.streams.active` | Binary streams executing now |
| `fastapi_canon.integrations.websockets.streams.duration` | Binary stream duration |
| `fastapi_canon.integrations.websockets.stream.frames` | Binary messages, split by `inbound` and `outbound` |
| `fastapi_canon.integrations.websockets.stream.bytes` | Binary bytes in each direction |
| `fastapi_canon.integrations.websockets.notifications` | Outgoing notification count by registered operation |
| `fastapi_canon.integrations.websockets.slow_consumers` | JSON-RPC slow-consumer closures |

Frame counters update while a stream is running and work independently of trace
sampling. They count binary application messages, including empty messages.
The input-end control message is excluded. Output counts follow successful
socket sends; input counts follow validation and enqueueing. Invalid or rejected
input and failed sends are excluded. Generator output and `RpcBinaryOutput`
writes share the same counting rules.

No span or retained trace event is created for each frame. Stream spans are
exported when the operation ends; use live metrics to observe long streams.
Normal disconnects and shutdown cancellations do not mark spans as errors.

## Write middleware

Subclass `RpcMiddleware` and override the lifecycle scopes or live activity
callbacks you need. `RpcMiddlewareLike` supports structural implementations of all three
lifecycle methods. The base class provides no-op implementations.

```python
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi_canon.integrations.websockets import RpcMiddleware, RpcStreamScope

logger = logging.getLogger(__name__)


class StreamTiming(RpcMiddleware):
    @asynccontextmanager
    async def stream(self, scope: RpcStreamScope) -> AsyncGenerator[None, None]:
        try:
            yield
        finally:
            outcome = scope.outcome
            if outcome is not None:
                logger.info(
                    "stream=%s duration=%s close=%s",
                    scope.endpoint.stream.name,
                    outcome.duration,
                    outcome.close_code,
                )
```

The three hooks are `connection(RpcConnectionScope)`,
`request(RpcRequestScope)`, and `stream(RpcStreamScope)`. Each returns an async
context manager. Scope endpoint metadata is exposed through the read-only
`RpcEndpointInfo` and `RpcStreamEndpointInfo` protocols. Requests also expose
their `RpcRequestContext` and registered method definition, where available.
Connection scopes begin with handshake metadata and gain their `connection`
during preparation. Stream scopes have an accepted connection on entry.

Before cleanup, the runtime assigns an immutable `scope.outcome` with the
duration, response or close information, original error, and cancellation
status. Stream outcomes retain errors caught in reader and writer tasks, even
after mapping them to close codes. Middleware enter and exit run in the same
task; batch items and concurrent requests use separate scopes.

Middleware composes from outermost to innermost: serving adapter, service,
endpoint. Each sequence enters in list order and exits in reverse order.
Configuration sequences are copied; operation state belongs in scopes or local
context-manager variables. Instrumentation failures are logged and isolated,
and middleware cannot suppress application failures or rewrite responses.
Cancellation propagates after cleanup.

`RpcRoutes` opens connection middleware before its `context=` dependency and
closes it after dependency cleanup, including mapped context rejections.
Earlier FastAPI or router-wide dependencies remain outside that boundary.
Manual routes start their boundary when they call `serve_websocket()`.

Custom `RpcRoutes.mount(handler=...)` handlers also report acceptance, close
codes, and connection lifetime when they use FastAPI's `WebSocket` directly.
RPC and stream scopes begin when a custom handler delegates to
`serve_websocket()`. Additional `middleware=` supplied there runs inside the
connection scopes already opened by the route, and participates in request,
stream, and activity callbacks. Instances already active on that connection
are reused without entering their scopes twice. Middleware added during
delegation finishes when delegated serving ends; route middleware also covers
the surrounding dependency cleanup.

When middleware is not configured, RPC requests and subscriptions bypass
operation scopes, outcome objects, and timing. Connections
and binary streams also skip middleware scopes, and FastAPI routes omit the
middleware dependency and custom-handler socket wrappers. To disable telemetry
with this fast path, omit `OpenTelemetry()` from `middleware=`; registering an
instance with both signals disabled still enters middleware scopes.

Generated-client instrumentation and OTEL log exporting are configured by the
application. Per-request trace propagation is described below.

[Back to documentation](README.md)
## Observe generated-client calls

Generated clients use `middleware` for instrumentation. Wrap
each attempt with a Python async context manager or a TypeScript `request(call,
next)` function, retain local span/metric state in that block, and inspect
`call.outcome` during teardown. Registration order nests middleware. The call
includes its route/server, selected transport when acquired, one-based attempt,
shared logical ID, JSON params, and duration in seconds. See the
[client middleware guide](clients.md#client-middleware-and-per-attempt-outcomes)
for timing, mutation, retry, and cancellation rules.

Outcome errors include remote/domain failures, connection/acquisition failures,
response validation, timeout, and caller cancellation. Failed attempts and
subscribe/unsubscribe control calls are observable. Keep tracing/export callbacks
cancellation-aware: the SDK preserves the original operation error if cleanup
also fails, while cleanup-only failures remain application errors and do not
retry. Success outcomes contain the validated result returned to the caller.
Log safe method/server/attempt/error-class fields; params, peer reasons, and
diagnostic causes can carry sensitive application or credential data.

## Per-request trace context

RPC requests and incoming notifications may include a top-level `meta` object:

```json
{
  "jsonrpc": "2.0",
  "id": 42,
  "method": "tasks.list",
  "meta": {
    "traceparent": "00-11111111111111111111111111111111-2222222222222222-01",
    "tracestate": "vendor=value"
  }
}
```

The only allowed keys are lowercase `traceparent`, `tracestate`, and `baggage`,
and all values must be strings. The compact JSON object, encoded as UTF-8, must
fit in **4096 bytes**, including keys and JSON syntax. Null, arrays, unknown keys,
non-string values, invalid Unicode, or excess size produce `invalid_request`
(-32600), without running the handler or adopting the supplied context. Invalid
notifications get no response. Each batch member has its own carrier and context.
The reserved heartbeat and cancellation controls do not carry this metadata.
Metadata never enters business params: there is no reserved `_meta` parameter,
no collision with existing models, and no need to add params to a no-argument
call. Existing named-param validation and positional-param policy are unchanged.

The server exposes a validated immutable mapping as `scope.metadata` and
`scope.request.metadata` on `RpcRequestContext`.
Absent metadata means `None`. With OpenTelemetry, an explicit carrier is extracted
as **remote W3C context** from a fresh context and parents that request's span.
When `connection_spans=True`, the request also links to its connection span.
Without metadata, current/handshake parenting continues as before. An empty
carrier, or a syntactically invalid `traceparent` inside an otherwise valid
carrier, starts a new root instead of inheriting the connection trace. Invalid
`tracestate` follows the OTel W3C propagator's handling. Request context is detached
on success, failure, and cancellation, including concurrent and batched calls.

OpenRPC advertises the convention in `x-rpc-request-metadata`:

```json
{
  "version": 1,
  "location": "envelope",
  "member": "meta",
  "maxBytes": 4096,
  "allowedKeys": ["traceparent", "tracestate", "baggage"]
}
```

Request-envelope schemas also describe `meta`. Regenerated clients enable sending
middleware metadata only for this exact capability. A nonempty carrier on a
client generated from an older or unsupported contract fails locally before send;
empty middleware carriers omit the extension. Non-fastapi_canon.integrations.websockets clients may participate
by following the advertised convention. Low-level transport users must check
support themselves. Deploy the server and regenerate its contract before enabling
client propagation; no handler signatures or existing parameter fields migrate.

### Python client recipe

Generated clients have no hard dependency on fastapi_canon.integrations.websockets or OpenTelemetry. Install
and configure OpenTelemetry in your application, then register this middleware
with your generated client's constructor or `connect(middleware=[Tracing()])`.
Replace `my_client` with the generated package:

```python
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind, StatusCode

from my_client import RpcClientCall

tracer = trace.get_tracer("my-client")


class Tracing:
    @asynccontextmanager
    async def request(self, call: RpcClientCall) -> AsyncGenerator[None, None]:
        with tracer.start_as_current_span(
            call.route.method, kind=SpanKind.CLIENT
        ) as span:
            carrier: dict[str, str] = {}
            propagate.inject(carrier)
            call.metadata.update(
                {
                    key: value
                    for key, value in carrier.items()
                    if key in ("traceparent", "tracestate")
                }
            )
            try:
                yield
            finally:
                if call.outcome is not None and call.outcome.error is not None:
                    span.set_status(StatusCode.ERROR)
```

Use a W3C trace-context propagator in the application. Each retry creates a new
attempt span under the caller's active context, shares the logical call ID, and
injects fresh metadata. Subscribe/unsubscribe controls run through the same
middleware; passive notification payload delivery does not. Python custom
transports implement the keyword-only
`metadata: Mapping[str, str] | None = None` in `request()` and serialize it as
`meta`. The single generated `RpcTransport` protocol includes this keyword.
The core supplies `metadata=None` even without tracing; update two-argument adapters.

### TypeScript client recipe

Install/configure the application OTel API, tracing SDK, W3C propagator, and a
context manager appropriate for Node or the browser. The generated SDK adds no
OTel import. This middleware forwards the active application context:

```ts
import { context, propagation } from "@opentelemetry/api";
import type { RpcClientMiddleware } from "./generated";

const tracing: RpcClientMiddleware = {
  async request(call, next) {
    const carrier: Record<string, string> = {};
    propagation.inject(context.active(), carrier);
    for (const key of ["traceparent", "tracestate"]) {
      if (carrier[key] !== undefined) call.metadata[key] = carrier[key];
    }
    await next();
  },
};

const client = await TasksClient.connect({ middleware: [tracing] });
```

`RpcRequestOptions.metadata` also accepts an explicit carrier; middleware receives
its per-attempt copy and may add or replace values. Custom TS transports receive
that carrier in their existing third options argument. Keep it out of params.

### Baggage

Remote baggage propagation is **off by default**, including handshake baggage.
Enable `OpenTelemetry(propagate_baggage=True)` only when the application needs it.
Client recipes must explicitly add `baggage` to their allowlist. The same size
bound applies. Baggage is untrusted remote data: do not use it for authorization
or automatically copy it into logs or span attributes. Locally established
application baggage is not stripped. Configure application propagators deliberately
and keep credentials out of tracing carriers.
