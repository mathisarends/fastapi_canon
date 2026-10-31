# Connections and events

`RpcConnection` is available as an injected dependency in methods and events.
Use it to inspect connection metadata or close an accepted connection. Perform
authentication in the hosting framework before calling fastapi_canon.integrations.websockets.

```python
from collections.abc import AsyncIterator

from fastapi_canon.integrations.websockets import (
    Inject,
    RpcChannel,
    RpcConnection,
    RpcModel,
    RpcService,
)

tasks = RpcChannel("tasks")
app = RpcService()
app.socket("/rpc", channels=(tasks,))
```

Headers are case-insensitive. `RpcConnection` also exposes `endpoint`, `path`,
`path_params`, `query_params`, `subprotocols`, `client`, `closed`, `close_code`,
`raw_close_code`, and `close_reason`. `close_code` is always a
`RpcConnectionClose` value; unknown peer codes become `OTHER` and remain
available as `raw_close_code`. Connection-scope finalizers can inspect these
fields to log why a socket ended.

Inject it like any other server-side dependency:

```python
@tasks.method()
async def connection_path(connection: Inject[RpcConnection]) -> str:
    return connection.path
```

After acceptance, injected code can close the live connection:

```python
from fastapi_canon.integrations.websockets import RpcConnection, RpcConnectionClose


@tasks.method()
async def sign_out(connection: Inject[RpcConnection]) -> None:
    await connection.close(RpcConnectionClose.NORMAL, reason="Signed out")
```

A `NORMAL` or `SHUTDOWN` close first sends the responses and notifications
already queued for the connection, within `RpcLimits.send_timeout` in total.
Other close codes close at once. Requests still running are cancelled, so their
responses are not sent.

## Map failures to rejections

A failure can end a connection instead of a call: a `before_accept` hook or a
context function refuses it, an event source or binary stream breaks. Declare
the [error bindings](errors.md#bind-domain-exceptions) of such failures in
`rejects=`, on `RpcService` for failures every endpoint shares and on
`socket()` or `stream()` for one endpoint's failures. Each binding names the
`RpcRejection` its exception becomes:

```python
from fastapi_canon.integrations.websockets import RpcErrorBinding, RpcRejection

session_expired = RpcErrorBinding(SessionExpired, rejection=RpcRejection.UNAUTHORIZED)
session_not_found = RpcErrorBinding(SessionNotFound, rejection=RpcRejection.NOT_FOUND)

app = RpcService(rejects=[session_expired])
app.socket(
    "/sessions/{session_id}/events",
    channels=(session_events,),
    rejects=[session_not_found],
)
```

The binding's message becomes the rejection reason, so a denied handshake
answers 404 with `Session not found` rather than the exception text. The same
binding can also appear in a method's `raises=`, where it answers calls with
the `session_not_found` error.

`serve()`, `create_router()`, `serve_websocket()`, `RpcRoutes`, and
`RpcTestClient` accept `rejections=` for call-specific policies: a mapping from
exception types to `RpcRejection` values, using the exception text as reason,
or a callable that returns an `RpcReject` or `None`:

```python
from fastapi_canon.integrations.websockets import RpcReject, RpcRejection


def reject(error: Exception) -> RpcReject | None:
    if isinstance(error, TaskAccessDenied):
        return RpcReject(RpcRejection.FORBIDDEN, "Access denied")
    return None
```

A failure is looked up in the call's policy first, then the endpoint's
`rejects=`, then the service's; the first one that maps it wins, and a callable
returning `None` passes it on. More specific levels therefore add or override
mappings without repeating the others.

The connection state decides what a rejection becomes:

| Failure | Result |
|---|---|
| `before_accept` hook, before the handshake | Handshake rejected (with the FastAPI adapter, an HTTP status such as 403 or 404) |
| Binary stream handler, after the handshake | Close with 1008 (`UNAUTHORIZED`, `FORBIDDEN`, `NOT_FOUND`), 1002, 1013, or 1011 |
| Event source, after the handshake | Same close codes, regardless of the event's `on_error` |
| JSON-RPC method | Unaffected; methods answer with JSON-RPC errors |

Raising `RpcReject` directly has the same effect without a mapping. Failures
the mapping does not cover keep their previous behavior.

## Notification delivery and reconnects

Notifications use at most once transport delivery: the SDK does not acknowledge
or durably replay them. Disconnects can lose messages, and a newly started remote
subscription can emit its own initial state again. Applications that need a
complete history should use event IDs, cursors, or an application-owned snapshot
and resync protocol.

With `reconnect=True` / `reconnect: true`, generated typed event iterators and
low-level notification iterators keep waiting across transient loss and resume on
the replacement transport. Remote subscriptions acquire a fresh subscription ID.
Unsubscribe stays on the transport that issued the old ID. Without reconnect,
transport errors still end the iterator. Permanent rejection or exhausted recovery
surfaces the terminal cause instead of waiting forever.

Use the optional [connection state callback](clients.md#lazy-connections-and-multiple-servers)
to detect gaps: every `reconnecting` transition means messages may be missing for
that server, and `connected` is the point to resync. Payload iterators keep their
existing types; there is no gap marker mixed into the stream. This convention
applies to both passive events and remote subscriptions.

The default overflow policy remains `drop_oldest` with one warning per transport
or logical subscriber. The shared transport queue and each consumer's queue are
bounded separately. `notification_queue_size` / `notificationQueueSize` now
configures both layers on connected clients; TypeScript also accepts it on clients
built with custom transports. A slow consumer can lose messages while another
consumer keeps up.

Observe loss with `on_overflow(method, dropped, source)` /
`onOverflow(method, dropped, source)`, typed as `RpcNotificationOverflowHandler`:

```python
def overflow(method, dropped, source):
    print(source, method, dropped)


async with TasksClient.connect(reconnect=True, on_overflow=overflow) as client:
    async for update in client.tasks.updated():
        ...
```

```ts
const client = await TasksClient.connect({
  reconnect: true,
  onOverflow: (method, dropped, source) => {
    console.log(source, method, dropped);
  },
});
```

`source` is `transport` or `subscriber`. `dropped` is cumulative for that buffer's
lifetime; a logical subscriber preserves its count across reconnects. Transport
counts reset for the new socket. A transport callback has no method (`None` /
`undefined`); subscriber callbacks identify their method, or `None` for a Python
wildcard consumer. Callback failures are contained and reported once per owner.

`client.dropped_notifications` / `client.droppedNotifications` counts subscriber
buffer drops across the client's lifetime. This counts buffer losses, so one
message dropped by two consumers contributes two. Each `WebSocketTransport`
exposes the same property name for its own shared queue's drop count. Use the
callback's source to distinguish these metrics. `notification_overflow="close"`
/ `notificationOverflow: "close"` still closes on shared queue overflow instead
of dropping data.

Iterator exit unregisters that consumer, including while reconnect is pending.
Explicit client close wakes waiting consumers and retires pumps and hubs.
TypeScript `iterator.return()` can interrupt a pending `next()`; custom TypeScript
transports can accept the optional notification `AbortSignal` to remove their
underlying waiters. Existing no-argument notification methods still type-check,
and the core stops its local wait if an older adapter ignores the signal. A custom
adapter owns cleanup of any upstream operation it leaves running.

Regenerate both SDKs to receive iterator recovery, overflow hooks and counters,
and cleanup. Default queue sizes, payload types, and reconnect opt-in behavior
remain unchanged.

## Push typed events

An event is an async generator. Each yielded Pydantic value becomes a JSON-RPC
notification sent to every connection on that endpoint:

```python
class TaskUpdated(RpcModel):
    task_id: int
    title: str


class TaskEvents:
    async def subscribe(self) -> AsyncIterator[TaskUpdated]: ...


@tasks.event(summary="Publish task changes.")
async def updated(events: Inject[TaskEvents]) -> AsyncIterator[TaskUpdated]:
    async for update in events.subscribe():
        yield update
```

The wire notification name is `tasks.updated`. Events accept only injected
parameters; callers do not subscribe with request parameters. A union of
Pydantic models is supported for event families, and a literal `type` field can
serve as their discriminator in generated clients.

The payload type comes from `AsyncIterator[T]`, so events do not need
`payload=`. The optional `@channel.event(payload=...)` only asserts that type
and fails at definition time when it differs from the yielded type.

Declare expected source failures with `@channel.event(raises=[binding])`.
Events inherit channel error declarations. A binding with `rejection=` closes
the connection using its own message and rejection before falling back to the
connection policies. With `on_error="close"`, a binding without a rejection
closes with `POLICY_VIOLATION` and its message; an unmapped failure closes with
`INTERNAL_ERROR` and is logged. The default `on_error="continue"` logs unexpected
source failures and keeps the connection open.

Events expect no answer. The server cannot call methods on the client; when it
needs input from the client, let the client call a server method instead.

### Finite event sockets

By default a socket stays open after its event sources finish. For a socket
that only reports a finite job, let the endpoint close it:

```python
@exports.event()
async def progress(job: Inject[ExportJob]) -> AsyncIterator[ExportProgress]:
    async for percent in job.run():
        yield ExportProgress(percent=percent)
    yield ExportProgress(percent=100, done=True)


app.socket(
    "/exports/{job_id}",
    channels=(exports,),
    close_when_events_complete=True,
)
```

Once every event source of the socket has finished, the queued notifications
are sent and the connection closes with `NORMAL`. A failing source closes it as
described in [Map failures to rejections](#map-failures-to-rejections). The
service rejects the option on a socket whose channels declare no events.

An event source can also close earlier with `await connection.close()`; the
notifications it yielded before are still delivered.

## Subscriptions with parameters

Declare an async generator when each connection needs its own filtered event
stream:

```python
class TaskFilter(RpcModel):
    project_id: str


@tasks.subscription()
async def changes(params: TaskFilter) -> AsyncIterator[TaskUpdated]:
    async for update in task_bus.listen(params.project_id):
        yield update
```

The generated Python client exposes `client.tasks.changes(project_id="demo")`
as an async iterator. Close the iterator promptly when leaving a loop early:

```python
from contextlib import aclosing


async with aclosing(client.tasks.changes(project_id="demo")) as changes:
    async for update in changes:
        if update.title == "Done":
            break
```

The generated TypeScript client accepts a typed params object. Breaking a
`for await` loop sends the unsubscribe request automatically.

On the wire, `tasks.changes.subscribe` returns a `subscriptionId`,
`tasks.changes` notifications carry `{subscriptionId, payload}`, and
`tasks.changes.unsubscribe` stops that generator. The generator's `finally`
block runs on unsubscribe and disconnect. `RpcLimits.max_subscriptions`
limits active subscriptions per connection (default 100). The OpenRPC document
describes these streams in `x-rpc-subscriptions`. Existing server events remain
broadcast notifications without subscription parameters.

## Instrument requests and connections

Register `RpcMiddleware` on a service or endpoint. Request and connection scopes
carry their final outcomes before teardown, and application state belongs to the
middleware instance:

```python
from contextlib import asynccontextmanager

from fastapi_canon.integrations.websockets import RpcMiddleware, RpcService, RpcSuccess


class GatewayMetrics(RpcMiddleware):
    @asynccontextmanager
    async def request(self, scope):
        logger.info("rpc started method=%s", scope.request.method)
        try:
            yield
        finally:
            logger.info(
                "rpc method=%s id=%s duration=%f success=%s",
                scope.request.method,
                scope.request.request_id,
                scope.outcome.duration,
                isinstance(scope.outcome.response, RpcSuccess),
            )

    @asynccontextmanager
    async def connection(self, scope):
        try:
            yield
        finally:
            logger.info("rpc disconnected code=%s", scope.outcome.close_code)

    async def connection_opened(self, connection):
        logger.info("rpc connected endpoint=%s", connection.endpoint)


app = RpcService(middleware=[GatewayMetrics()])
```

Scope failures are logged without replacing the serving error. `RpcMiddleware`
provides no-op scopes and live activity callbacks; `RpcMiddlewareLike` supports
structural lifecycle implementations. Requests expose `scope.request.connection`
for socket calls and `None` for a standalone server. Optional callbacks
`notification_sent(name, size)`, `stream_frame_sent(connection, size)`,
`stream_frame_received(connection, size)`, and `slow_consumer_closed(connection)`
report live activity and backpressure; size counts bytes. Use lifecycle scopes
for completion rather than separate start/finish or connection-close callbacks.
See [Observability](observability.md) for timing, failure policy, and OTel setup.

## Runtime limits

Configure per-connection backpressure and message limits with `RpcLimits`:

```python
from fastapi_canon.integrations.websockets import RpcLimits

limits = RpcLimits(
    max_concurrency=16,
    max_queue_size=64,
    max_message_bytes=512_000,
)
```

`max_concurrency` bounds in-flight calls, `max_pending_requests` bounds calls
that are running or waiting for a slot (further calls are answered with a
`pending_limit` error), `max_queue_size` bounds outgoing responses and events,
and `max_message_bytes` rejects oversized incoming frames. Calls on one connection may finish out of order; set concurrency to
`1` when strict arrival order is required. Pass `None` only when intentionally
disabling the byte limit.

[Back to documentation](README.md)
## Draining connections

Each service has a `service.drain` coordinator; pass `RpcService(drain=RpcDrain())`
to share one coordinator across services. `await service.drain.drain(timeout=30)`
is idempotent and permanently moves that coordinator into shutdown. The first
call chooses the budget; cancellation of a caller waiting for drain does not
cancel the shared drain. `draining` and `connection_count` expose its state.

New connections are unavailable (WebSocket 1013; adapters may deny the handshake
with HTTP 503). New ordinary calls on accepted sockets receive the built-in
`server_shutting_down` error. Pings and cancellation controls remain usable
while admitted requests finish. Passive event sources stop immediately. Active
subscriptions and dedicated binary streams and sockets may
finish naturally until the grace deadline; unfinished work is cancelled.

The final tenth of the timeout is reserved for cooperative cleanup. Admitted
responses and the outgoing queue flush during the first nine tenths; a blocked
flush cannot extend the total budget. Connections close with
`RpcConnectionClose.SERVICE_RESTART` (1012), which middleware also sees.
Existing disconnect or explicit-close causes are preserved during races.
Handlers, dependency exits, and middleware cleanup must cooperate with cancellation;
if cleanup exceeds the final deadline, drain returns, cancels it again, and logs
the outstanding cleanup. Python cannot forcibly terminate cancellation-resistant
application code. Create a fresh coordinator for a new service lifetime.

Generated reconnect policies already treat 1001, 1012, and 1013 as transient.
The request error is a remote error and is not automatically replayed. Client
operation signatures and defaults are unchanged by server drain support.
## Authentication and credential expiry

Authentication remains application-owned. Validate credentials in `before_accept`
and return your identity as a dependency alongside an expiry:

```python
from fastapi_canon.integrations.websockets import RpcConnectionExpiry, RpcHandshake


async def authenticate(handshake: RpcHandshake):
    identity = await verify_bearer(handshake.headers.get("authorization"))
    return {
        Identity: identity,
        RpcConnectionExpiry: RpcConnectionExpiry(identity.expires_at),
    }
```

`Identity` and `verify_bearer` are application types/functions. Raise `RpcReject`
with unauthorized/forbidden when authentication fails. Never include the token
in its reason. `expires_at` is a finite Unix timestamp in **seconds**, not a
monotonic deadline or JavaScript milliseconds. After acceptance, the runtime
converts the remaining wall-clock duration once to the event loop's monotonic
clock. Past timestamps expire immediately; later wall-clock adjustments do not
move that timer. In an injected `RpcConnection`, use `connection.expire_at(ts)`
or assign `connection.expires_at`; `None` removes the timer. Changing it replaces
the previous timer, and disconnect cancels it. Renewal requires your application
to verify renewed authorization before extending the expiry.

All endpoint types close with policy violation (1008) and the exact reason
`credentials_expired`. Both SDK reconnect policies narrowly recognize that pair
as transient. Other policy reasons stay terminal. For immediate revocation,
`await connection.close(RpcConnectionClose.POLICY_VIOLATION,
reason="credentials_revoked")` closes with a terminal reason. Your application
can call it after a revocation event or identity check; fastapi_canon.integrations.websockets does not maintain
an identity/revocation registry. Close reasons and middleware state contain the
constant reason, never credentials.

Python/server-to-server clients can use an async `headers` factory:

```python
async def headers():
    return {"Authorization": f"Bearer {await credentials.refresh()}"}


connection = ApiClient.connect(
    headers=headers,
    reconnect=True,
    reconnect_max_attempts=3,
)
```

It runs again for each connection attempt and new dedicated socket. Refresh with
async I/O, or offload blocking credential work rather than blocking the loop.
Visible handshake 401/403 or ordinary 1008 rejection terminates recovery.

Browser WebSockets accept a URL and subprotocols, not custom authorization
headers. Obtain a short-lived, preferably single-use connection ticket through
your authenticated HTTPS API, then use the TypeScript `credentials` provider:

```typescript
const client = await ApiClient.connect({
  reconnect: true,
  credentials: async ({ url, subprotocols }, signal) => {
    const ticket = await fetchConnectionTicket({ signal });
    const authenticated = new URL(url);
    authenticated.searchParams.set("ticket", ticket);
    return { url: authenticated, subprotocols };
  },
});
```

The provider receives a copy of the URL and an attempt AbortSignal; it runs for
every RPC handshake/reconnect, and for newly opened binary streams and sockets. Only
RPC sockets reconnect automatically. It can return replacement `url` and/or
`subprotocols`; omitted fields retain the resolved endpoint. Pool attempt/call
timeouts also bound refresh waiting. Honor the signal to stop your own I/O;
late provider results cannot open a socket after pool close. Direct
`WebSocketTransport.open()` also accepts `credentials`, but callers must supply
their own opening deadline/signal.

When TypeScript `connect()` has a provider and no explicit reconnect attempt
limit, recovery permits three connection attempts. Browser handshake errors may
hide HTTP status, so this finite default prevents rejected refresh from looping
indefinitely. Visible 401/403/404 or generic 1008 still terminates immediately.
`reconnectMaxAttempts` overrides the budget; `null` deliberately removes it.
Without a provider, existing reconnect defaults are unchanged. Request retry is
separate: refreshing a socket never implies replaying a sent operation.

Use `wss://` in production. Query tickets can appear in proxy/access logs; redact
query credentials and URL telemetry, keep tickets short-lived, and avoid putting
long-lived bearer credentials in the URL. Cookie authentication needs an exact
Origin allowlist and explicit cross-site connection controls, plus suitable
Secure/HttpOnly/SameSite cookie policy. Cookie presence alone does not authorize
the initiating origin. Subprotocol credential carriers must use legal HTTP token
syntax (for example base64url without padding), retain the actual RPC protocol in
the offered list, and let the server negotiate that protocol; do not echo a secret
carrier as the selected protocol. These requirements follow the
[WebSocket handshake rules](https://www.rfc-editor.org/rfc/rfc6455#section-4.1)
and [OWASP WebSocket guidance](https://cheatsheetseries.owasp.org/cheatsheets/WebSocket_Security_Cheat_Sheet.html).

Normalized SDK connect-error messages do not copy raw exception strings or URLs.
Original `cause` / `__cause__` remains available, and third-party exceptions or
peer-supplied reasons can contain sensitive data: log safe class/status/code
fields and apply redaction before exporting diagnostic causes or state errors.
Regenerate TypeScript clients for the provider API; Python header signatures
remain compatible. Expiry and revocation are opt-in server policies.
