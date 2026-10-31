# SDK resilience and migration

This guide covers connection lifecycle, capacity limits and SDK resilience.
Use the linked guides for configuration and the [changelog](../CHANGELOG.md)
for version-specific breaking changes.

## What existing applications need to know

Regenerate **both SDKs from the new OpenRPC document**. Runtime behavior is
embedded in generated code; upgrading only the server does not update deployed
client runtimes. Edit generators/templates, then regenerate instead of editing
generated files:

```bash
uv run canon websockets generate --config rpcgen.toml
uv run canon websockets generate --config rpcgen.toml --check
```

Existing ordinary Python method signatures stay unchanged. TypeScript methods
gain an optional trailing `RpcRequestOptions` argument with `signal` and
`metadata`. Required business params stay required. Static TypeScript model
types still do not perform full runtime schema validation; custom routes can
provide a synchronous `validateResult` callback. Python retains Pydantic checks.

Malformed response envelopes now fail the connection instead of being silently
ignored. Valid late/duplicate replies remain harmless. Deep invalid server input
is normalized into protocol errors with bounded logging. Pending work is counted
per request or notification, including executing and queued batch members; a
batch no longer consumes one slot for many calls. Review `RpcLimits` for your
traffic: defaults include 32 concurrent handlers, 1024 pending items, 32 batch
members, 128 queued messages, a 1 MiB message bound, and a 10-second send timeout.
See [Services and limits](services.md).

Import FastAPI APIs from the `fastapi_canon.integrations.websockets` package root. Dishka and OTel
integrations live under `fastapi_canon.integrations.websockets.adapters.*`; follow the migration
guide for removed predecessor modules.

## Defaults and opt-ins

| Feature | Default | How to opt in or change it |
|---|---|---|
| Server request deadline | Unlimited | `RpcLimits(request_timeout=...)`; method `timeout=` overrides, `None` disables inheritance |
| SDK request budget | 30 seconds, now includes acquisition | Connection `request_timeout` / `requestTimeoutMs` |
| Reconnect | Disabled | `reconnect=True` / `reconnect: true` |
| Recovery attempt limit | Unlimited | `reconnect_max_attempts` / `reconnectMaxAttempts`; zero disables recovery |
| Method retry | Zero additional attempts | `retry_attempts` / `retryAttempts`; optional per-attempt budget |
| TypeScript heartbeat | 30-second idle interval, 10-second timeout for an exact advertised capability | `heartbeatIntervalMs`, `heartbeatTimeoutMs`; null interval disables |
| TypeScript timeout breaker | Disabled | `consecutiveTimeoutLimit` |
| Connection expiry | No expiry | Return `RpcConnectionExpiry(unix_seconds)` from `before_accept`; renew with `connection.expire_at()` |
| TypeScript credential refresh | No provider | Async `credentials` provider; when configured, recovery defaults to three attempts unless explicitly overridden |
| Client middleware | Empty | `middleware` on construction, transport setup, or connect |
| Trace metadata | Omitted | Middleware `call.metadata` with the advertised contract capability |
| Remote baggage | Disabled | Server `propagate_baggage=True`, plus explicit sender allowlisting |

Python timeout/backoff settings use **seconds**; TypeScript names ending in `Ms`
use **milliseconds**. Credential expiry uses Unix seconds. Middleware outcome
durations use seconds in **both** SDKs. Reconnect attempt limits count physical
connection openings in a recovery episode; request retry settings count extra
method attempts after the first. Initial connect failures are still returned
without the proactive recovery loop.

## Error handling, cancellation, and retry

Catch `RpcRemoteError` for server/domain failures. Local failures have matching
Python and TS names: `RpcConnectError` exposes permanent/status/code information,
`RpcConnectionLostError` exposes `sent`, code, and reason, and
`RpcRequestTimeoutError` exposes the method and budget. Python also retains
response/notification validation errors; TypeScript custom result validators
report `RpcResponseValidationError`. Original causes remain available, so avoid
logging them indiscriminately when URLs or credentials may be present.

`sent=False` means the transport knows the request was not sent. `sent=True`
means delivery is uncertain; it does not prove that the server executed or failed
to execute the call. Opt-in retries may repeat unsent calls. Ambiguous loss or an
attempt timeout may repeat only methods declared `idempotent=True`. Mark a method
idempotent only when repeating it is safe for your application. Every attempt
uses a fresh wire ID and consumes the same overall request budget. This does not
add deduplication or exactly-once execution.

Caller cancellation keeps Python `CancelledError` or the exact TS abort reason.
After a sent call is abandoned, the SDK sends best-effort `$/cancelRequest` on its
original socket. Server deadlines/cancellation include admission and queued work,
but handlers must cooperate with cancellation. Remote errors, validation errors,
middleware failures, explicit close, and caller cancellation never cause ordinary
automatic retry. Subscription controls and binary/raw streams do not enter ordinary method retry. See [Client cancellation and retry](clients.md)
and [Protocol controls](protocol.md).

## Connection recovery and notification gaps

With reconnect enabled, transports recover proactively with shared connection
attempts and full-jitter backoff. `state`, per-server `states`, and state callbacks
describe connecting/connected/reconnecting/closed. `client.closed` now describes
terminal closure and stays pending through recoverable drops. Explicit close
interrupts opening, backoff, waiting iterators, and SDK-owned cleanup.

Passive notification iterators resume and remote subscriptions obtain fresh
server subscription IDs. Delivery remains at-most-once: reconnect can lose events,
and bounded queues can drop the oldest item. Use state transitions to resync your
application and `on_overflow` / `onOverflow` plus dropped counters to detect local
loss. Durable replay, sequence numbers, and acknowledgements remain application
responsibilities. See [Notification delivery](connections-and-events.md#notification-delivery-and-reconnects).

## Shutdown and credentials

Call `await service.drain.drain(timeout=...)` through an authenticated pre-stop
path **before** process termination. It rejects new connections, denies new
ordinary calls with `server_shutting_down`, stops passive producers, and allows
admitted work/replies to finish within the budget. Remaining sockets close with
1012 so reconnecting clients can recover. Uvicorn lifespan teardown alone happens
too late to initiate this grace period. See [Graceful drain](transports.md).

Use short-lived credentials and refresh them per physical opening. Python async
header providers already run per attempt; TypeScript async `credentials` providers
can supply fresh URL tickets/subprotocols. Server expiry closes with exactly
`1008 / credentials_expired`, which is recoverable with reconnect enabled. Generic
1008 policy rejection, visible permanent auth failures, and revocation remain
terminal. Browser cookies/tickets, Origin checks, token redaction, and credential
renewal are described in [Connection authentication](connections-and-events.md).

## Middleware and tracing

Middleware is the only instrumentation API. Client middleware runs once
per physical attempt, with a shared logical ID, attempt number, copied JSON params,
selected transport, and success/error/duration outcome. Python uses async context
managers; TS uses onion middleware with exactly one `await next()`. Inspect outcomes
in teardown/finally. Cleanup failures preserve the original operation error;
cleanup-only failures remain application errors. All attempts share one validation
path, and success outcomes contain the validated result. See [Client middleware](clients.md#client-middleware-and-per-attempt-outcomes).

`call.metadata` is separate from business params. The top-level `meta` carrier
accepts only W3C `traceparent`, `tracestate`, and optional `baggage` strings within
4096 compact UTF-8 bytes. Explicit remote context parents a request span and links
to an optional connection span; absent metadata retains handshake behavior. Older
contracts reject nonempty client carriers locally.
OTel is an application opt-in, with no new generated runtime dependency. Follow
the [Python/TypeScript tracing recipes](observability.md#per-request-trace-context)
and keep remote baggage out of authorization and automatic logging.

For resilience tests, `fastapi_canon.integrations.websockets.testing.SocketFaults` and `InMemorySocketFactory`
provide deterministic disconnects, blocked sends, delayed/held responses,
half-open sockets, and handshake rejection. Use controlled clocks and events to
test your own retry, refresh, resync, and shutdown policy.

## Breaking instrumentation cleanup

Remove `hooks` / `RpcClientHook` and use client `middleware`. Before work belongs
in middleware entry; after work belongs after `yield` / `await next()` or in
finally. Middleware runs per physical attempt, including retries. Server
`observer=` and `RpcObserver` / `RpcObserverLike` are removed; use `RpcMiddleware`
request/connection scopes and their outcomes. Live activity callbacks remain on
`RpcMiddleware`. The observer module and its response/connection context types
have no replacement aliases.

Custom Python adapters accept `metadata=` through the single `RpcTransport`
interface; `RpcMetadataTransport` is removed. Catch `RpcConnectionLostError`
instead of the unused `RpcConnectionClosed`. TypeScript clients use
`onStateChange` instead of `onDisconnect`; direct transports expose `closed`.
Pool openers take `(endpoint, signal)`. See the migration section in
[CHANGELOG.md](../CHANGELOG.md) for the complete list of breaking changes.
