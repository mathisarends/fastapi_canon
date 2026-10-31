# Contract and clients

The service definition is the source of truth. fastapi_canon.integrations.websockets exports it as OpenRPC,
then generates clients from that document rather than from Python internals.
The resulting artifact can therefore be reviewed, versioned, and consumed by
other tooling.

Install the generator extra:

```bash
uv add "fastapi-canon[websockets,codegen]"
```

## Build a contract

```python
from fastapi_canon.integrations.websockets import ServerVariable

contract = app.contract(
    title="Tasks API",
    description="Realtime task operations.",
    base_url="https://{host}",
    variables={
        "host": ServerVariable(
            default="api.example.com",
            description="API host",
        )
    },
)
```

Endpoint paths, names, subprotocols, protocol version, typed errors, and binary
streams are derived from the service. `base_url` accepts either an HTTP or
WebSocket URL (or a URL template); `http` and `https` are translated to `ws`
and `wss`. Every supplied variable must occur in the resulting server URLs.

Use `contract.write("tasks.openrpc.json")` to write canonical UTF-8 JSON, or
`contract.to_json()` to get the same text. `contract.to_openrpc()` returns the
complete document as a dictionary for integrations that need one.

## One configuration for contract and clients

Put the importable contract in a module, then create `rpcgen.toml` beside your
project paths:

```toml
version = 1

[contract]
source = "my_api:contract"
output = "schema/tasks.openrpc.json"

[[clients]]
language = "python"
output = "src/tasks_client"
package = "tasks_client"
with_transport = "websocket"
extra_files = ["src/extra/authenticated.py"]
extra_exports = ["connect_gateway"]

[[clients]]
language = "typescript"
output = "frontend/src/tasks-client"
with_transport = "websocket"
```

Generate everything together:

```bash
canon websockets generate --config rpcgen.toml
```

Paths are resolved relative to the config file. Client generation reads the
rendered OpenRPC document; it does not receive the live `RpcService` or
`RpcProtocol` object.

Run the same command in CI with `--check`. It exits nonzero and lists stale
files instead of writing them:

```bash
canon websockets generate --config rpcgen.toml --check
```

Generated directories are owned by the generator and should not be edited by
hand. `extra_files` copies Python source files into the generated package by
filename; `extra_exports` adds uniquely defined names from those files to the
package root and manifest. This keeps custom connection helpers across
regeneration.

## Generate from an existing document

The schema and client stages can also run separately:

```bash
canon websockets schema my_api:contract \
  --output schema/tasks.openrpc.json

canon websockets generate schema/tasks.openrpc.json \
  --language python \
  --output src/tasks_client \
  --package tasks_client \
  --with-transport websocket
```

Omit `with_transport` when integrating with an existing transport. Python
clients accept an `RpcTransport`; TypeScript clients accept the corresponding
transport interface. The OpenRPC generators are tolerant of ordinary tagged
OpenRPC methods and additionally understand fastapi_canon.integrations.websockets's typed-error and binary
stream extensions.

## Use a generated client

The generated package root exports the client, models, namespace classes,
declared error classes, endpoint metadata, routes, and transport building
blocks. Applications normally need only the client and their domain models.

With the generated WebSocket transport, Python exposes an async connection
context:

```python
from tasks_client import TasksClient

async with TasksClient.connect(host="api.example.com") as client:
    task = await client.tasks.create(title="Write docs")
    async for update in client.tasks.updated():
        print(update)
```

Pass handshake headers directly when authentication is transport-owned:

```python
async with TasksClient.connect(
    host="api.example.com",
    headers={"Authorization": f"Bearer {token}"},
) as client:
    ...
```

For refreshed credentials, pass an async header factory. It runs for each
WebSocket connection, including binary stream connections:

```python
async def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {await tokens.current()}"}


async with TasksClient.connect(headers=auth_headers) as client:
    ...
```

An HTTP or HTTPS `url=` override is converted to WS or WSS for the RPC socket.

When the client outlives a context manager, await the connection directly. The
caller then owns the returned client and must close it:

```python
client = await TasksClient.connect(host="api.example.com")
try:
    ...
finally:
    await client.close()
```

TypeScript connects explicitly and should be closed when no longer needed:

```ts
const client = await TasksClient.connect({ host: "api.example.com" });
try {
  const task = await client.tasks.create({ title: "Write docs" });
} finally {
  await client.close();
}
```

TypeScript projects with explicit resource management can express the same
lifecycle with `await using`:

```ts
await using client = await TasksClient.connect({ host: "api.example.com" });
const task = await client.tasks.create({ title: "Write docs" });
```

Closing a generated client is idempotent and closes every transport the client
owns.
Python custom-transport clients wait for all owned transports to close and
ignore individual close failures, matching clients opened through `connect()`.
Cancelling the caller's `close()` still propagates cancellation.

The request timeout covers sending the request and waiting for its response.
Valid late, duplicate, or unknown responses are ignored, so a timed-out or
cancelled call does not interrupt other calls on the same connection. Malformed
responses still fail the transport. If a Python timeout or cancellation occurs
while `send()` is blocked, the transport is retired: delivery is uncertain and
the socket cannot safely be reused. Socket shutdown waits for the adapter's
close operation; configure its close timeout to bound that cleanup. This follows
the [websockets send cancellation guidance](https://websockets.readthedocs.io/en/stable/reference/asyncio/connection.html#websockets.asyncio.connection.Connection.send).

Generated clients answer unexpected requests from the server with
`method_not_found` and keep the connection open. Regenerate existing clients to
receive these transport fixes. Existing calls still work, and the default 30-second timeout
is unchanged. TypeScript methods also accept trailing request options.

## Cancel a request

The generated WebSocket transports send the [request cancellation control](protocol.md#request-cancellation)
when a sent call times out or is abandoned. It uses the same socket and original
request ID, and is never replayed onto a replacement connection. Cancellation is
best effort: a lost socket or completed server invocation can prevent an abort.
The client removes local pending state immediately and ignores the server's valid
late `request_cancelled` reply. Other calls remain usable on the same connection.

In Python, cancel the task running the generated method. Its outcome stays
`asyncio.CancelledError`, including the cancellation message:

```python
import asyncio

call = asyncio.create_task(client.tasks.create(title="Write docs"))
await user_cancelled.wait()
call.cancel("Cancelled by the user")
try:
    await call
except asyncio.CancelledError:
    pass
```

A configured `request_timeout` still raises the SDK's `RpcRequestTimeoutError`.
For an application-owned per-call budget, `asyncio.timeout(seconds)` around a
generated call also triggers this cancellation path; that outer context raises
Python's `TimeoutError`. Generated method signatures are unchanged, so an RPC
parameter named `timeout` keeps its existing meaning.

Python sends controls in background tasks with a one-second send bound and at
most 128 simultaneous control sends per transport. A full control capacity skips
that best-effort send. Errors are consumed, and close or disconnect cancels and
awaits outstanding sends. Cancellation before sending emits no control. If the
original request send is interrupted, the transport is retired because delivery
is uncertain; it cannot safely carry a subsequent control.

TypeScript methods accept an optional trailing `RpcRequestOptions` object:

```ts
const controller = new AbortController();
const call = client.tasks.create(
  { title: "Write docs" },
  { signal: controller.signal },
);
controller.abort();
try {
  await call;
} catch (error) {
  if (error !== controller.signal.reason) throw error;
}
```

Methods without params accept the options directly, such as
`client.tasks.list({ signal })`. For all-optional params, pass an empty params
object first: `client.tasks.search({}, { signal })`. An already aborted signal
rejects before middleware, connection acquisition, or request sending. Abort during a
hook or connection wait rejects locally; shared connection acquisition can finish
for other callers. The signal's exact `reason` is preserved, including the
default `AbortError`; abort failures are not wrapped in RPC error classes.
Timers and abort listeners are removed on every terminal path.

Low-level TypeScript calls accept
`transport.request(method, params, { signal })` and
`core.request(route, params, { signal })`; use `undefined` for absent params.
Custom transports should honor this third argument to stop remote work. Existing
two-argument transport implementations remain structurally compatible; the core
can abandon its local wait even if such an adapter ignores the signal. The built-in
WebSocket factory uses the native synchronous `send()` contract, so control sends
create no background promises or timers. A control-send failure preserves the
original abort or timeout.

Configure the existing connection-level `request_timeout` / `requestTimeoutMs`
to change the default budget; TypeScript has no per-call timeout override yet.
Regenerate both SDKs to receive automatic cancellation and the TypeScript options
API. Existing call sites and timeout defaults are compatible.

## Lazy connections and multiple servers

Contracts may route different methods to different WebSocket servers. A
generated client still presents one namespace tree and selects the declared
server for each call. `connect()` opens every declared server in parallel, so
authentication and network failures occur at connection time. Pass `lazy=True`
or `lazy: true` to open each socket when a method routed to it is first called;
concurrent first calls share the same connection attempt:

```python
async with TasksClient.connect(host="stage.example.com", lazy=True) as client:
    ...
```

```ts
const client = await TasksClient.connect({
  host: "stage.example.com",
  lazy: true,
});
```

Generated connection clients expose `client.state`, per-server `client.states`
snapshots, and `client.closed`. Python exports the `RpcConnectionState` string
enum; TypeScript uses the same four string values: `connecting`, `connected`,
`reconnecting`, and `closed`. Unopened lazy endpoints start as `connecting`.
The aggregate state prefers `reconnecting`, then `connected` if any endpoint is
available; a terminal endpoint or explicit client close makes it `closed`.

Pass `reconnect=True` / `reconnect: true` to recover proactively after a transient
drop, including when no method call is waiting. Initial connect failures surface
immediately and are terminal; they are never retried. Subsequent recovery uses
one shared attempt per server. Cancelling or timing out one caller leaves the
attempt available to others. Each attempt is bounded by the configured request
timeout, and explicit close interrupts opening and backoff. Eager connection
failure closes the other endpoints without waiting for an unlimited opener.

`client.closed` resolves with the first terminal cause, or `None` / `undefined`
after explicit close. With reconnect enabled it stays pending during transient
loss and recovery. Without reconnect, the first drop remains terminal. If one
server becomes terminal, the aggregate signal resolves and its state is `closed`,
while other servers keep their own transports and can still serve calls until
explicit close. A closed client borrowing a source resolves its local signal
without closing that source.

```python
async with TasksClient.connect(reconnect=True) as client:
    cause = await client.closed
    if cause is not None:
        print(f"Client terminally closed: {cause}")
```

State callbacks identify the endpoint and preserve its original cause:

```python
def state_changed(server, state, error):
    print(server, state)


async with TasksClient.connect(
    reconnect=True,
    reconnect_max_attempts=5,
    on_state_change=state_changed,
) as client:
    ...
```

```ts
const client = await TasksClient.connect({
  reconnect: true,
  reconnectMaxAttempts: 5,
  onStateChange: (server, state, error) => {
    console.log(server, state);
  },
});
```

Callbacks run in state-transition order per server: initial `connecting`, then
`connected`; a retryable drop emits `reconnecting`, then `connected` or `closed`.
Keep callbacks quick and synchronous. Callback failures are reported and contained.
Intentional close also reports `closed`; inspect the server and error arguments
to distinguish affected endpoints and failures. Every reconnect can imply a notification gap; resync application
state when connectivity returns. Remote subscriptions reacquire their server
subscription, and passive event iterators also resume on the replacement socket.
See [Notification delivery](connections-and-events.md#notification-delivery-and-reconnects)
for gap detection and overflow counters.

Backoff uses full jitter: a random fraction between zero and one times the current
delay cap, which doubles from `reconnect_initial_delay` / `reconnectInitialDelayMs`
up to `reconnect_max_delay` / `reconnectMaxDelayMs`. Caps reset after success.
The first recovery attempt is immediate; jitter applies between failed attempts.
`reconnect_max_attempts` / `reconnectMaxAttempts` counts attempts in each recovery
episode; the default `None` / `null` is unlimited, and zero disables recovery.
Exhaustion preserves the last connect error. `reconnect_random` / `reconnectRandom`
accepts an injectable fraction source for deterministic tests.

Permanent `RpcConnectError` failures, including visible 401/403/404 handshake
rejections, stop immediately. Disconnects with 1001, 1012, or 1013 are transient.
Protocol/data/policy/size closes 1002/1003/1007/1008/1009 are terminal by default,
as are malformed RPC frames. The sole default policy exception is **1008 with
the exact reason `credentials_expired`**, which requests credential refresh;
other 1008 reasons remain terminal. Python's header factory runs on every attempt.
Invalid refreshed credentials that visibly reject with 401/403 stop recovery.
Browsers can expose only an opaque handshake error, so configure a finite attempt
limit when that prevents reliable permanence classification.

Override classification with `reconnect_should_retry(error)` /
`reconnectShouldRetry(error)` for an application-specific policy. A classifier
failure stops retrying and preserves the connection error. Automatic reconnect
opens a new transport; it never replays the failed RPC request.

The configured request timeout now covers connection acquisition, shared-attempt
wait, backoff, middleware entry/body, and sending/waiting for the RPC result in
one budget. It does not reset after connection acquisition. SDK timeout types and the 30-second default stay unchanged;
cancellation of a sent call still uses the ticket 06 wire control. Regenerate both
SDKs to receive this policy and the new lifecycle members. Generated root APIs
must avoid names `closed`, `state`, and `states`; alias colliding namespaces with
the generator's `api_names` option.

Top-level server variables such as `host` apply to every server and binary
stream that declares that variable. Override only exceptional deployments with
the generated server names:

```python
from tasks_client import ServerName, TasksClient

async with TasksClient.connect(
    host="stage.example.com",
    servers={
        ServerName.MEDIA: "wss://media.stage.example.com/rpc",
    },
) as client:
    ...
```

```ts
const client = await TasksClient.connect({
  host: "stage.example.com",
  servers: { media: "wss://media.stage.example.com/rpc" },
});
```

An endpoint override may also be a generated `Endpoint` object when its URL and
subprotocols need to be supplied together. Unknown server names and values
outside a variable's declared enum are rejected instead of being silently
accepted.

The generated `endpoints.<server>()` factories are the typed escape hatch for
that advanced case. Each factory exposes only the variables declared by its
server and preserves the server's default subprotocols:

```python
from tasks_client import ServerName, TasksClient, endpoints

async with TasksClient.connect(
    servers={
        ServerName.MEDIA: endpoints.media(host="media.stage.example.com"),
    },
) as client:
    ...
```

```ts
import { endpoints, TasksClient } from "./tasks-client";

const client = await TasksClient.connect({
  servers: {
    media: endpoints.media({ host: "media.stage.example.com" }),
  },
});
```

The map key and the endpoint returned by the factory must name the same server.

## Custom transports and middleware

Use `with_transports()` in Python or `withTransports()` in TypeScript for tests
and custom adapters. Pass either one transport for a single-server contract or
a transport map keyed by the generated server names. Set `close_transport=False`
or `closeTransport: false` when another component owns those transports.

Client middleware observes and wraps each attempt through the `middleware` option
on `connect()` and the custom-transport constructor. Socket factories are also
injectable, which keeps the generated runtime independent of a particular
WebSocket package and makes connection behavior testable without a network.

## Errors on the client

Generated Python and TypeScript packages export the same client error hierarchy:

```text
RpcClientError
  RpcRemoteError (declared and built-in server errors)
  RpcResponseValidationError
  RpcNotificationValidationError (Python)
  RpcTransportError
    RpcRequestTimeoutError
    RpcConnectionLostError
    RpcConnectError
```

Python response/notification validation errors and stream errors also inherit
`RpcClientError`. TypeScript's existing remote, connection-close, and stream
errors now share that base. Catch `RpcClientError` for SDK failures, or the
existing `RpcTransportError` base for transport failures. Cancellation and
application hook exceptions propagate with their original semantics.

`RpcRequestTimeoutError` carries `method` and `timeout`; the timeout is in seconds
in Python and milliseconds in TypeScript. `RpcConnectionLostError` carries
`sent`, `code`, and `reason`. `sent=False` means this call provably never began a
socket send. Once send begins, including a send that raises before completing,
`sent=True` means the server may have received and executed it. Retry such a call
only when the application knows it is safe. This is per-call state; two calls on
the same failed connection can have different delivery certainty.

```python
from tasks_client import RpcConnectionLostError, RpcRequestTimeoutError

try:
    await client.tasks.create(title="Write docs")
except RpcRequestTimeoutError as error:
    print(f"{error.method} exceeded its {error.timeout}s timeout")
except RpcConnectionLostError as error:
    print(f"Connection lost: {error.code} {error.reason}; may be sent: {error.sent}")
```

Connection setup failures raise `RpcConnectError`. Its `permanent` flag is true
for observed HTTP 401/403/404 or policy close code 1008; other failures remain
unclassified as permanent. HTTP `status` and close `code` are retained when
available. Python extracts those fields from the socket library without importing
its exception classes. Browser WebSockets generally hide handshake HTTP status,
so opaque events leave `status` undefined and `permanent` false; an injected
factory may provide more information. The flag is classification metadata for
reconnect policy; this change does not add a new retry policy.

Original transport errors are chained as Python `__cause__` or TypeScript `cause`.
Request failures, notification/subscription iterators, and the WebSocket
transport's `closed` result preserve SDK error types. TypeScript now exposes
`transport.closed`, resolving with the typed failure or `undefined` after explicit
normal close; Python keeps its existing future, with `None` after normal close.

Regenerate SDKs to obtain these types and runtime changes. Existing Python
`except RpcClientError` / `except RpcTransportError` clauses continue to catch the
new subclasses; code that caught raw socket exceptions should move to the client
hierarchy. Generated schemas cannot shadow the new exported runtime error names.

The exact client class, method arguments, endpoint names, and server variables
come from the document. See
[`examples/generated_clients`](../examples/generated_clients) for complete
generated Python and TypeScript packages.

[Back to documentation](README.md)
# Connection keepalive

Generated TypeScript `connect()` enables application heartbeats when the OpenRPC
document advertises `x-rpc-heartbeat` with `method: "$/ping"`, `params: {}`, and
`result: null`. An idle connection sends one JSON-RPC request with an integer ID,
`method: "$/ping"`, and empty object params. The server returns `result: null`.
Defaults are `heartbeatIntervalMs: 30_000` and `heartbeatTimeoutMs: 10_000`.
Set `heartbeatIntervalMs: null` to disable, or a positive value to opt in for a
known compatible server. Direct `WebSocketTransport.open()` leaves probing off.
Missing or different capability metadata leaves it off; a valid error reply to a
probe also disables it without disconnecting. Regenerate clients to acquire the
capability default. Existing operation signatures and Python clients are unchanged.

Any valid inbound traffic resets idle detection and the timeout breaker. With
`reconnect: true`, heartbeat failure produces `RpcConnectionLostError` and enters
the normal state/reconnect policy. Only one probe is outstanding. If browser
timers resume much later than a probe deadline, a fresh probe gets a new timeout;
there is no backlog of heartbeats. Detection takes interval plus timeout while
timers run normally; background suspension can extend that bound.

`consecutiveTimeoutLimit` is an optional fallback (default `0`, disabled). A
positive integer fails the socket after that many consecutive sent-request
timeouts without valid inbound traffic. Choose it cautiously for slow servers;
it does not imply an operation failed to execute. Caller aborts do not count.

Python's default `websockets` factory uses its protocol-level keepalive (20 s ping
interval and timeout). A custom socket factory must provide its own keepalive;
the generated Python runtime does not add application probes.
## Opt-in request retries

Set `retry_attempts=2` (Python) or `retryAttempts: 2` (TypeScript) on `connect`
to allow two retries after the initial attempt: at most three total attempts.
The default is zero. Use `reconnect=True` / `reconnect: true` for connection
recovery. Constructors and `with_transports` / `withTransports` also accept the
retry settings for custom adapters.

`RpcConnectionLostError(sent=False)` permits retry for any ordinary method.
When delivery may have occurred (`sent=True`), or an attempt times out, only a
route with contract-declared idempotency permits retry. Remote/domain errors,
response validation failures, permanent connect errors, caller cancellation,
and explicit client close never trigger retry. Unknown delivery is conservatively
sent. Streams and subscriptions do not enter this loop.

One request timeout bounds connection acquisition, wire work, and retry backoff.
The deadline is never restarted. An optional `retry_attempt_timeout` (seconds) /
`retryAttemptTimeoutMs` bounds each attempt, including acquisition, and can leave
time for another attempt within the overall deadline. Without it, a transport
timeout can permit retry only while the overall budget remains. Retries reuse
params and allocate a fresh request ID; connection pools reuse their configured
initial/max delays and jitter source for retry backoff. Custom sources can expose
`retry_delay` / `retryDelay`, otherwise the default full jitter is 0.25–5 seconds
(the actual wait ranges from zero to the capped delay).

After ambiguous delivery, the original handler might still be running. Retry can
therefore execute idempotent work concurrently. Middleware runs once per attempt
and carries its one-based attempt number plus a shared logical call ID. Regenerate both
SDKs to acquire retry options and idempotency metadata; method signatures and
existing execution behavior remain unchanged until retry is enabled. Idempotency
keys for non-idempotent work are a separate future extension.
## Client middleware and per-attempt outcomes

Pass `middleware=` / `middleware` to constructors, `with_transports` /
`withTransports`, or `connect`. The default is empty. Python middleware supplies
an async context manager; TypeScript supplies `request(call, next)` and awaits
`next()` exactly once. Registration order is outer-to-inner; teardown reverses it.

```python
from contextlib import asynccontextmanager


class Metrics:
    @asynccontextmanager
    async def request(self, call):
        local_state = start_metric(call.route.method)
        try:
            yield
        finally:
            finish_metric(local_state, call.outcome)
```

```typescript
const metrics = {
  async request(call: RpcClientCall, next: () => Promise<unknown>) {
    const localState = startMetric(call.route.method);
    try {
      await next();
    } finally {
      finishMetric(localState, call.outcome);
    }
  },
};
```

Import the call/middleware types from the generated package. Metric functions are
application-owned. `route`, JSON `params`, `server`, `transport`, one-based
`attempt`, and `call_id` / `callId` are available on each call. Each retry has a
fresh scope and the same logical ID, which is local correlation metadata rather
than a wire ID. Failed acquisition leaves `transport` unset. After the body,
`outcome` records result or original error and `duration`; `call.duration` reads
the same value. Durations use **seconds in both SDKs**, start after initial JSON
normalization, and include middleware entry, connection acquisition, send/wait,
and response validation. They exclude retry backoff and normal teardown. For
subscription control calls, the iterator has already acquired its transport;
that acquisition is outside the subscribe/unsubscribe call scope.

Caller cancellation keeps `CancelledError` and its message in Python, and the
exact AbortSignal reason in TypeScript. A whole-call timeout during an attempt
is observable as `RpcRequestTimeoutError`. If teardown also fails, the original
error wins and the cleanup failure is logged; after a successful body, a teardown
failure becomes the application error. Middleware application errors never cause
automatic retry, even when their class is otherwise retryable. Python managers
cannot suppress RPC failures. TypeScript ignores replacement return values and
requires `next()` once, so instrumentation cannot silently skip or duplicate a
wire call. Async middleware and custom adapters must cooperate with cancellation.

Each attempt receives its own JSON params copy. Python validates modified params
against the generated params adapter before send. TypeScript keeps its generated
static types and validates serialization at the transport boundary; it does not
gain a full runtime schema validator. Custom TypeScript routes may provide a
synchronous `validateResult` callback; failures become `RpcResponseValidationError`
and are visible to middleware. Python keeps Pydantic response validation. Treat
outcome/routing fields as read-only; mutate `params` before yielding/calling next.
Keep per-request tracing metadata separate from user params.

Middleware is the only client instrumentation API. Use it for per-attempt metrics,
errors, and local span state; group attempts by logical ID for logical-call metrics.
A deadline exhausted during backoff occurs outside an attempt scope. Subscribe and
unsubscribe scopes are observable but never enter ordinary method retry. Passive
notification payload delivery does not create request scopes.

All calls use the same attempt/validation pipeline, including an empty middleware
chain. Successful outcomes contain the validated result returned to the caller.
For subscriptions, Python validates the control acknowledgement before reading
its subscription ID. Regenerate both SDKs and migrate `hooks` to `middleware`;
there are no compatibility callbacks, raw-result wrappers, or deprecated aliases.
Python and TypeScript SDKs have no dependency on tracing.

Client middleware can mutate `call.metadata`, an initially empty string carrier
separate from business params. For compatible contracts, the SDK validates and
sends it as the top-level `meta` extension. Older contracts reject nonempty
carriers locally. TypeScript also accepts `RpcRequestOptions.metadata`.
See [Per-request trace context](observability.md#per-request-trace-context) for
Python/TypeScript OTel recipes, bounds, and custom adapter changes.
