# Changelog

## 0.4.1 - 2026-10-25

[GitHub release and artifacts](https://github.com/mathisarends/canon/releases/tag/fastapi-rpckit-v0.4.1)
and [PyPI](https://pypi.org/project/fastapi-rpckit/0.4.1/).

### Performance

- Validate each socket request envelope once. The reader's validated envelope is
  reused for dispatch and subscription handling instead of being validated again,
  which saves about 3 µs per request. Middleware still receives the original raw
  request, and error responses for invalid requests are unchanged. Wire output
  is unchanged; JSON encoding stays on the standard library because
  pydantic-core's serializer formats some floats differently.

### Added

- `scripts/bench_rpc_requests.py` measures sequential round-trip latency and
  pipelined request throughput over the in-process socket runtime. See
  [Measure request throughput](docs/transports.md#measure-request-throughput).

### Documentation

- Add a core-API overview to the README and use the workspace's locked root
  commands for development. Replace the stale version note with links to the
  changelog and the `pyrpckit` migration guide.
- Refresh `AGENTS.md`: current module map, locked workspace commands and one
  consistent compatibility rule (preserve wire behavior and generated-client
  contracts; remove superseded Python APIs only through a breaking release).

## 0.4.0 - 2026-10-20

### Breaking changes

- Generated `WebSocketFactory`, `SocketConnectionFactory` and
  `BinaryWebSocketFactory` protocols accept their URL as a positional-only
  argument. Regenerate Python clients and pass the URL positionally when invoking
  these factories. This matches the existing generated runtime calls and permits
  custom factories with differently named first parameters. Custom factories
  with named first parameters continue to work.
- `serve_websocket()` propagates caller task cancellation after endpoint cleanup.
  Direct callers that cancel and await this helper must handle `CancelledError`.

### Fixed

- Preserve the original ASGI cancellation while awaiting endpoint cleanup. This
  prevents teardown stalls with minimum AnyIO and Starlette versions and avoids
  leaking child-task cancellations through current Starlette TestClient sessions.
- The generated default connectors adapt `websockets`' nominal `Subprotocol` type
  inside their lazy backend boundary. Factory protocols remain independent of
  `websockets`; consumers no longer need generator overrides for this mismatch.

## 0.3.0 - 2026-10-20

### Breaking changes

- Remove server-to-client RPC. Every RPC method is now implemented by the
  server and called by the client. `channel.client.method()`, `RpcClientMethod`,
  `RpcConnectedClient`, the `RpcClientMethod*Error` exceptions,
  `RpcClientClosedError`, `RpcProtocol.client_methods`,
  `RpcLimits.client_method_timeout` and `RpcTestClient(client_methods=...)` /
  `RpcTestClient.handle()` are gone.
- Remove `channel.server`. Declare operations directly on the channel with
  `@channel.method()`, `@channel.event()`, `@channel.subscription()`,
  `@channel.stream()` and `@channel.socket()`. These were already the
  default spelling; `channel.server.X` was an alias.
- OpenRPC documents no longer contain `x-rpc-client-methods` or
  `*ClientMethod` request schemas, and the generators reject documents that
  still declare `x-rpc-client-methods`.
- Generated Python clients no longer contain `handlers.py`,
  `internal/client_methods.py`, `current_client_method_metadata()`, the
  `connect(handlers=...)` option, the transport's `request_handler=` option
  or the `create()` class methods on client-method errors. Like TypeScript
  clients, they answer any request the server sends with `method_not_found`.
- The server no longer accepts JSON-RPC response frames from the client. They
  are answered with `invalid_request` like any other malformed request.
- Raw sockets are now sockets. A socket is a transport endpoint; the messages
  it carries belong to the handler. Migrate as follows:
  - `@channel.raw_socket(...)` becomes `@channel.socket(...)` with the same
    options.
  - `service.raw_socket(path, handler, ...)` becomes
    `service.socket(path, handler, ...)`. `service.socket(path, channels=...)`
    still mounts JSON-RPC endpoints; passing both a handler and `channels=`, or
    JSON-RPC-only options with a handler, raises `ProtocolDefinitionError`.
  - Inject `RpcSocket` instead of `RpcRawSocket`; `RpcRawMessage` is
    `RpcSocketMessage` and `RpcRawSocketEndpoint` is `RpcSocketEndpoint`.
    The module `fastapi_rpckit.raw_sockets` is now `fastapi_rpckit.sockets`.
  - The low-level WebSocket adapter protocol formerly named `RpcSocket` is now
    `RpcWebSocket`. Rename custom adapters and annotations that implemented or
    referenced the old protocol; `FastApiSocket` is unchanged.
- The contract lists sockets under `x-rpckit-sockets` instead of
  `x-rpckit-raw-sockets`. Re-export contracts and regenerate clients together;
  generators reject contracts that still contain `x-rpckit-raw-sockets`.
- Generated clients rename their socket API. Python: module `sockets` (was
  `raw_sockets`), `Socket`, `SocketMessage`, `SocketMessageType`, `SocketName`,
  `SocketEndpoint`, `SocketTransport`, `SocketOpener`, `SOCKETS`,
  `SocketConnection`, `SocketConnectionFactory`, `SocketConnectionStream`;
  `connect(socket_connection_factory=...)` and `socket_opener=...` replace
  `raw_socket_factory` and `raw_socket_opener`. TypeScript: module `sockets`
  (was `rawSockets`), `Socket`, `sockets`, `resolveSocketEndpoint`,
  `SocketConnectionStream`, and the options `socketConnectionFactory`,
  `socketQueueSize`, and `socketOpener` replace `rawSocketFactory`,
  `rawSocketQueueSize`, and `rawSocketOpener`. Regenerate clients and rename
  these references in consuming code.
- Server log messages and close reasons say "socket" instead of "raw socket".
  Socket wire behavior, close codes, and message framing are unchanged.
- Typed JSON input is now validated before queueing. A malformed later message
  can therefore cancel an active invocation. Clients must send valid messages
  throughout a connection rather than depending on an earlier reply before an
  invalid queued message is rejected.

Migration:

1. Replace `@channel.server.method(...)` and the other `channel.server.*`
   decorators with `@channel.method(...)` and the matching channel decorator.
2. Replace each server-to-client call with a server method the client calls,
   an event or a subscription. Remove `Inject[RpcConnectedClient]` parameters.
3. Drop `client_method_timeout=` from `RpcLimits(...)` and `client_methods=`
   from `RpcTestClient(...)`.
4. Regenerate clients and remove handler classes and `handlers=` arguments
   from `Client.connect(...)` calls.
5. Rename `@channel.raw_socket` to `@channel.socket`, `service.raw_socket(...)` to
   `service.socket(...)`, `RpcRawSocket` to `RpcSocket`, and the adapter
   protocol `RpcSocket` to `RpcWebSocket`, then re-export contracts and
   regenerate clients as described above.

### Added

- Infer typed JSON connections from `Inject[RpcSocket[Incoming, Outgoing]]`,
  including aliases and unions. These handlers retain their own receive/send loop,
  unsolicited messages, multiple replies, and one dependency scope per connection.
  They share cached internal Pydantic validation and serialization with message
  handlers. Annotations alone define message types, directional contract schemas,
  and Python/TypeScript SDK types. Bare `RpcSocket` keeps raw text/binary behavior;
  protocol metadata never enables validation.
- Declare JSON socket message handlers as `async def ping(message: Ping) -> Pong`.
  The library validates each input, calls the handler sequentially in a fresh
  invocation scope, and validates and serializes its response. Invalid input
  closes with 1002 and invalid output with 1011, without exposing message contents.
  Injecting `RpcSocket` keeps the existing connection-handler text/binary behavior.
  Protocol metadata alone still does not enable validation.
- Export directional socket message schemas and generate Python and TypeScript
  `Socket[Received, Sent]` clients. Add a checked, live `typed_socket` example.
  Python SDK validation uses generated models; TypeScript supplies static payload
  types and JSON framing, with authoritative validation on the server.

- Declare the application protocol of a socket with
  `@channel.socket(..., protocol=RpcSocketProtocol(...))`. The declaration
  holds a protocol name, version, encoding, and specification URI references.
  It is metadata only: messages still pass through unchanged. Handlers read it
  as `RpcSocket.protocol`.
- Contracts export declared protocols as `x-rpckit-protocol` on socket entries.
  The generator IR keeps them as `SocketProtocolDecl`, whose
  `resolve_schemas()` resolves relative schema references against the contract
  location. Generated Python and TypeScript clients expose them as
  `SocketProtocol` on socket endpoints.
- Plug typed protocol clients into code generation with
  `fastapi_rpckit.codegen.bindings`. A `ProtocolBinding` selected by protocol
  name emits a module into the generated client, and matching socket methods
  return its session instead of a generic `Socket`. Select bindings with
  `bindings = [...]` in the generator config, `--binding`, or the
  `fastapi_rpckit.socket_bindings` entry point group. Sockets without a
  binding keep the generic `Socket`.
- Add the `socket_protocol` example: a small CDP-shaped protocol with Python and
  TypeScript bindings, tested against a live server.

### Fixed

- Close an opened TypeScript socket when a protocol binding fails to initialize,
  including asynchronous failures. Preserve the initialization error if closing
  the transport also fails.
- Keep bindings with identical exported factory or session names separate in
  generated Python and TypeScript clients by importing their modules under
  distinct aliases.

## 0.2.0 - 2026-10-18

### Breaking changes

- `dishka_router` now exposes explicit keyword options and rejects an async
  function passed as a context value. Its context is a fixed value or mapping;
  mount per-connection FastAPI context dependencies with
  `RpcRoutes(router, context=async_dependency, resolver=Dishka(), rejects=...)`.
  Check wrappers that forward options and migrate context factories to RpcRoutes.
- Classify these source API/validation changes as breaking even though JSON-RPC
  wire contracts are unchanged. The prepared patch version 0.1.1 was never
  published and is superseded by 0.2.0. Before 1.0, breaking changes always require
  the next minor version with patch zero; release validation enforces this rule.

### Added and corrected

- Allow a custom per-WebSocket resolver factory on `dishka_router`.
- Add a tested cookie/Origin authentication example with typed handshake
  rejections, Dishka APP/SESSION/REQUEST scopes and connection expiry.
- Use package-root FastAPI imports in documentation and fix the optional Dishka
  installation diagnostic. `RpcRoutes` remains the public mounting name;
  nested imports remain available.
- Update metadata, root pins, lockfile and both generated-client manifests to
  0.2.0. The earlier 0.1.1 dry-run evidence is historical and does not validate
  this version's artifacts. Corrected source `0a2b097` passed four-target CI
  and the [0.2.0 dry run](https://github.com/mathisarends/canon/actions/runs/39127145156);
  downloaded wheel/sdist hashes match the manifest. Publication jobs were skipped.

## 0.1.0 - 2026-10-17

- Use `fastapi-rpckit` / `fastapi_rpckit` after PyPI rejected the earlier local
  `fastapi-wsrpc` name for similarity. This is the first successor release.
- Fork pyrpckit 0.11.0 into the Canon workspace as `fastapi-rpckit`, importing
  `fastapi_rpckit` and using the `fastapi-rpckit` CLI.
- Require FastAPI and export `RpcRoutes`, `FastApiSocket`, `create_router`, and
  `serve_websocket` from the package root. Keep Dishka and codegen optional.
- Preserve the RPC engine, JSON-RPC wire protocol, `x-rpckit-*` OpenRPC extensions,
  binary/raw channels, and generated client APIs. Regenerate example clients.
- See [migration steps](docs/migration.md). Existing repositories remain available.
- Retain 0.1.0 for the first successor release: the inherited pyrpckit 0.11.0
  engine and generated client contracts are preserved. Source parity and consumer
  verification are recorded in the workspace documentation before publication.
- Fix Python SDK formatting for long discriminator defaults, large event unions,
  and route annotations discovered with Cara's exported contract. No generated
  names, signatures, validation, or wire payloads change; regenerate clients.

The entries below are the predecessor's history and retain their original names.

## 0.11.0 - 2026-10-14

### Release hardening

- Subscription capacity is reserved while acknowledgements are queued and stays
  occupied during unsubscribe cleanup. Cancelled handshakes release their slots.
  Connection cancellation and drain deadlines now always stop subscription tasks
  before releasing connection dependencies.
- Heartbeat replies use bounded response collectors, so a blocked outgoing queue
  cannot stop the reader from processing request cancellation or reverse replies.
- Server deadlines and client cancellation cover middleware entry and exit.
  Interrupted middleware produces the normal typed failure reply and exposes that
  same failure to previously entered middleware during cleanup.
- Credential expiry interrupts accepted-connection initialization, including
  activity callbacks, connection scopes, and binary/raw dependency resolution.
  Expired connections close with `1008 / credentials_expired` even before their
  normal reader and writer tasks have started.
- Python SDK close interrupts active acquisition, requests, and middleware even
  when the source is borrowed. A late acquisition cannot send after close; caller
  cancellation remains distinct, and borrowed sources remain open. Regenerate
  Python clients to receive the fix.
- Uninstrumented Python SDK attempts skip middleware call objects, UUIDs, and
  redundant deep copies. The same request and result validators serve both paths;
  retry attempts remain isolated from parameter mutations by custom transports.
- Single-request socket responses avoid unnecessary collector tasks and completed
  task joins. Subscription control lookup no longer scans every subscription for
  each ordinary request.
- Subscription acknowledgements are final: a deadline or cancellation during
  middleware teardown cannot produce a second reply for the same request ID.
- Request IDs reject booleans and floats instead of coercing them to integers,
  preventing collisions with other in-flight requests. Oversized JSON integers
  produce parse errors; Python clients classify them as protocol errors rather
  than reconnectable connection failures.

### Migration from 0.10

- Send request IDs as strings, integers, or `null`. Boolean IDs and floating-point
  IDs such as `1.0` now produce `Invalid Request`; use `1` or `"1"` instead.
- Server request deadlines now include middleware entry and teardown. Keep both
  cancellation-cooperative and move unrelated background work out of the request
  scope. An acknowledged subscription stays active if handshake teardown times out.
- Client instrumentation now uses only `middleware`. Remove `hooks`,
  `RpcClientHook`, `before_request` / `beforeRequest`, and `after_response` /
  `afterResponse`. Move before work into middleware entry and after work into
  teardown/finally around `yield` (Python) or `await next()` (TypeScript).
  Middleware runs per physical attempt, including retries and subscription
  controls; correlate logical calls with `call_id` / `callId`. All attempts use
  the same validation path, and successful outcomes contain the validated result.
  Middleware entry/body run within the overall request budget; backoff remains
  outside an attempt scope. There are no hook adapters or deprecation aliases.
- Server instrumentation now uses only `middleware=`. Remove `observer=` and
  `RpcObserver` / `RpcObserverLike`; `rpckit.observer` is removed. Replace
  request-start/finish callbacks with `RpcMiddleware.request(scope)` and
  connection-close callbacks with `connection(scope)`, reading `scope.outcome`
  in teardown. `RpcResponseContext` and `RpcConnectionContext` are removed;
  request metadata stays on `RpcRequestContext` through `scope.request`.
  Live acceptance, notification, frame, and slow-consumer activity callbacks
  belong to `RpcMiddleware` and remain available there.
- Python custom transports implement the single generated `RpcTransport.request`
  interface, including keyword-only `metadata: Mapping[str, str] | None = None`.
  The core always supplies this keyword. Update two-argument adapters;
  `RpcMetadataTransport` and its compatibility dispatch branch are removed.
- Replace the unused SDK `RpcConnectionClosed` error with
  `RpcConnectionLostError` in catches/imports. TypeScript `onDisconnect` is
  removed; use `onStateChange` for client lifecycle transitions and
  `transport.closed` for direct transports. State callbacks also report intentional
  closure and identify the server. `RpcTransportPool.open` receives only
  `(endpoint, signal)` and tracks disconnects through `transport.closed`.
- Optional integrations now live in `rpckit.integrations`. Replace
  `rpckit.fastapi` with `rpckit.integrations.fastapi`, `rpckit.dishka` with
  `rpckit.integrations.dishka`, and `rpckit.opentelemetry` with
  `rpckit.integrations.opentelemetry`. The old modules have been removed without
  compatibility aliases.

### Added

- Bounded per-request W3C tracing metadata in the top-level `meta` envelope
  extension, advertised as `x-rpc-request-metadata` in OpenRPC. Allowed string
  fields are `traceparent`, `tracestate`, and `baggage`, with a 4096-byte compact
  UTF-8 bound. Request scopes expose an immutable carrier; business params and
  no-param calls keep their shape. Explicit context parents RPC spans and links
  to optional connection spans; absent metadata keeps handshake behavior.
  `OpenTelemetry(propagate_baggage=True)` opts into remote baggage. Both SDKs
  expose middleware `call.metadata`, validate it before send, and reject nonempty
  carriers for older contracts. Python reverse calls accept `metadata=`, the
  optional integration provides `trace_metadata()`, and generated handlers expose
  task-local `current_client_method_metadata()`. SDK tracing recipes require
  application-installed OTel only; no generated runtime dependency is added.
  Service-restart drain closes are normal telemetry outcomes. The complete
  hardening stack and migration defaults are explained in
  `docs/hardening-overview.md`.
- Both SDKs expose per-attempt `RpcClientMiddleware`, `RpcClientCall`, and
  `RpcClientCallOutcome`: shared logical IDs, JSON params, routing/transport,
  success/error, and duration in seconds. Scopes cover wire work, acquisition,
  validation, and subscription control calls; cancellation reasons and original
  failures survive cleanup errors. Middleware is the only instrumentation API.
  Mutated Python params are revalidated; TypeScript custom routes can supply
  `validateResult` and receive `RpcResponseValidationError`. Middleware faults
  never trigger automatic request retry.
- `RpcConnectionExpiry` from `before_accept`, plus `RpcConnection.expires_at`
  and `expire_at()`, bound long-lived credentials using Unix seconds and a
  monotonic timer. RPC, binary, and raw sockets close with exactly
  `1008 / credentials_expired`; timers retire on disconnect. Python async header
  providers refresh on reconnect. TypeScript adds async `credentials` providers
  for URLs/subprotocols on RPC reconnect and new dedicated sockets, with three
  recovery attempts by default when a provider is configured. Generic 1008 and
  visible credential rejection remain terminal. Normalized connection messages
  omit raw exception text; original causes remain available for diagnostics.
- `RpcDrain` coordinates service shutdown across RPC, binary, and raw sockets.
  It rejects new connections, returns built-in `server_shutting_down` for new
  requests, stops passive producers, and lets admitted work and queued replies
  finish within a bounded grace period. Remaining work closes with the new
  `SERVICE_RESTART` (1012); new handshakes use unavailable (1013 or HTTP 503).
  A real Uvicorn shutdown test verifies an explicit drain before process stop;
  lifespan teardown alone is final cleanup, not the pre-shutdown trigger.
- Methods can declare `idempotent=True`, exported as `x-rpc-idempotent` and
  generated route metadata in both SDKs. Optional `retry_attempts` /
  `retryAttempts` (default zero extra attempts) retry unsent calls and declared
  idempotent calls after ambiguous connection loss or an attempt timeout.
  Retries use the reconnect jitter policy, fresh wire IDs, and one overall call
  deadline; optional attempt timeouts leave room within that budget. Caller
  cancellation, explicit close, remote errors, and validation errors are excluded.
- OpenRPC advertises the reserved `$/ping` heartbeat. The server answers it
  outside handler capacity, middleware, and request observation. Generated
  TypeScript clients enable idle heartbeats (30 s interval, 10 s timeout) only
  for contracts advertising the exact capability; unsupported replies disable
  probing. Heartbeat settings and a disabled-by-default consecutive-timeout
  breaker are configurable. Dead sockets enter the existing reconnect lifecycle,
  and paused browser timers recheck liveness with one fresh probe.
- Generated passive event and notification iterators resume after transient loss
  when reconnect is enabled, alongside remote subscriptions. Connection state
  callbacks signal possible gaps without changing payload types; no durable
  replay is introduced. Retired hubs are removed, consumers share one pump per
  live transport, and close/iterator exit wakes pending waits. TypeScript iterator
  return can interrupt reconnect acquisition and notification waiting.
- Notification overflow is observable through `RpcNotificationOverflowHandler`,
  `on_overflow` / `onOverflow`, and dropped-message counters on clients and
  WebSocket transports. Counters distinguish subscriber and shared transport
  buffers, preserve logical subscriber totals across reconnects, and keep
  one-time warnings. TypeScript accepts configurable subscriber queue sizes and
  optional notification abort signals for adapter cleanup. Regenerate SDKs;
  default sizes, drop-oldest policy, and payload iterator types remain compatible.
- Generated clients expose connection `state`, per-server `states`, and state
  callbacks. Reconnect uses shared, cancellable connection attempts, full-jitter
  backoff, optional attempt limits, and overridable classification. Recovery runs
  proactively after transient loss; permanent handshake/policy failures stop with
  their original cause. Close codes 1001/1012/1013 are transient, with an exact
  1008 `credentials_expired` exception for credential refresh. Header factories
  run per attempt. Regenerate SDKs; reconnect remains opt-in and attempts unlimited
  by default. Initial failures remain unretried, and failed RPC calls are not replayed.
- `client.closed` now waits for terminal closure during reconnect instead of
  resolving on a transient drop. Explicit close interrupts opening/backoff and
  ends a local client even when its source is borrowed. One terminal endpoint
  resolves the aggregate signal while other endpoint transports remain independent.
  Request budgets now cover connection acquisition and the wire call together;
  the 30-second default and typed timeout errors remain unchanged.
- Generated WebSocket clients send best-effort `$/cancelRequest` controls after
  sent calls time out or are cancelled. Python retains `CancelledError` and tracks
  bounded background control sends through cleanup. TypeScript exposes
  `RpcRequestOptions` with `signal?: AbortSignal` on transports, core requests,
  and trailing generated method options; abort reasons retain their identity.
  Controls stay on the original transport, and settlement clears pending state,
  timers, and abort listeners. Regenerate SDKs; existing calls and the default
  30-second timeout remain compatible. Python method signatures are unchanged.
- Opt-in server deadlines through `RpcLimits.request_timeout` and per-method
  `timeout=` overrides. The default remains unlimited; explicit method `None`
  disables inheritance. Monotonic budgets include admission scheduling, semaphore
  wait, dependencies, and execution; timeout errors are built-in `request_timeout`
  failures. Subscription handshakes share the budget while streams remain unbounded.
- RPC socket request cancellation through validated `$/cancelRequest`
  notifications, with typed IDs and per-item batch tracking. Controls bypass
  admission and blocked response queues, returning built-in `request_cancelled`
  (-32800) after cleanup. Duplicate in-flight IDs close with a protocol error;
  disconnect and shutdown retain outer cancellation without cancellation replies.
  OpenRPC documents the control and explicit method budgets. Generated SDK method
  signatures remain unchanged and expose these server failures as remote errors.
- Matching generated Python and TypeScript client error hierarchies with
  `RpcRequestTimeoutError` (`method`, `timeout`), `RpcConnectionLostError`
  (`sent`, `code`, `reason`), and `RpcConnectError` (`permanent`, `status`, `code`).
  Transport exceptions retain their cause; delivery is conservatively uncertain
  once send begins. TypeScript exposes a typed `transport.closed` promise and
  shares `RpcClientError` / `RpcTransportError` bases across SDK errors. Existing
  Python base catches keep working. Regenerate clients; caller cancellation and
  application hook exceptions retain their semantics. Retry policy is unchanged.
- Public `SocketFaults`, `InMemoryClientSocket`, and `InMemorySocketFactory` in
  `rpckit.testing` for deterministic resilience tests. Script directional drops,
  frame delays with an injected clock, held responses, half-open connections,
  blocked sends, and next-connect handshake rejections. Client and server views
  share the existing in-memory queues and preserve close metadata, including
  abnormal disconnects without emitting reserved close code 1006. Factory scopes
  clean up connections and serving tasks; `RpcTestHandshakeRejected` exposes
  scripted status/code/reason. Existing in-memory defaults remain fault-free.
- Lifecycle middleware for connections, requests, and binary streams through
  `RpcMiddleware` and `RpcMiddlewareLike`. Register `middleware=` on services,
  endpoints, standalone servers, or FastAPI adapters. Scopes expose final
  outcomes, including rejection, original failures, and cancellation, before
  cleanup. Middleware composes in order and preserves independent context for
  concurrent requests. The final middleware-only API replaces observers.
- Optional `pyrpckit[otel]` integration through
  `rpckit.integrations.opentelemetry.OpenTelemetry`, using application providers
  for RPC and stream spans, connection measurements, active-operation metrics,
  and live frame and byte counters. Reuse active trace context or extract it from
  handshake headers; configure exclusions, tracing, and metrics independently.
  Payloads and raw unknown method names are not captured automatically.
- Mount application-owned connection handlers with
  `RpcRoutes.mount(endpoint, handler=...)`. Async handlers support `Inject[T]`
  for endpoint context and resolver dependencies alongside regular FastAPI
  parameters. Routes retain endpoint paths, contract metadata, and rejection
  policies; handlers own socket acceptance, messages, and closure. Dishka
  dependencies share a connection SESSION scope with the endpoint context.
- Add an observability guide covering middleware scopes, OpenTelemetry setup,
  signal configuration, and stream metric accounting.
- Raw socket endpoints for native WebSocket protocols such as the Chrome
  DevTools Protocol. Declare them with `@channel.raw_socket(...)` and mount
  them with `RpcService.raw_socket(...)`; handlers inject `RpcRawSocket`, which
  passes text and binary messages through unchanged. rpckit owns acceptance,
  message type and size limits, backpressure, and closure, and cancels the
  handler on disconnect so forwarding tasks and upstream connections are
  released. `closes=` binds domain exceptions to close codes and reasons,
  including failures inside task groups; unmapped failures close with 1011
  without exposing their messages. Raw sockets work with `RpcRoutes`,
  `create_router()`, Dishka, middleware, and `RpcTestClient`
  (`send_message()` / `next_message()`).
- The contract describes raw sockets in `x-rpckit-raw-sockets` with URLs,
  variables, message types, subprotocols, and close outcomes. Generated Python
  and TypeScript clients expose them as `client.<namespace>.<name>()`, returning
  a `RawSocket` that sends and iterates `str`/`bytes` (`string`/`ArrayBuffer`)
  messages, with `raw_socket_factory` / `rawSocketFactory` connect options and
  `raw_socket_opener` / `rawSocketOpener` for custom transports.

### Changed

- Requests and subscriptions without middleware skip observation
  scopes, outcomes, and timing. Uninstrumented streams and FastAPI routes also
  avoid middleware scopes, extra dependencies, and custom-handler socket wrappers.
- Annotate async context-manager generators as `AsyncGenerator[T, None]` and
  synchronous context-manager generators as `Generator[T, None, None]`, including
  middleware, documentation, and generated Python stream helpers.
- Generated TypeScript clients define `RpcStreamClosed`, `RpcStreamFailed`,
  `RpcStreamRefused`, and `RpcStreamsUnavailableError` in `core.ts`, shared by
  binary streams and raw sockets; the package index still exports them. Their
  messages now say "stream" instead of "binary stream".

### Fixed

- `pending_limit` remains a built-in error under `strict_errors=True`, matching
  deadline and cancellation failures. Bound rejection and malformed-message
  response collectors keep the socket reader available for cancellation.
- Served RPC connections reserve pending capacity per request or notification,
  including each batch member, so batches cannot multiply
  `RpcLimits.max_pending_requests`. Admission is per item in wire order; excess
  calls retain batch response shape, notifications remain unanswered, and
  server-to-client responses bypass admission. Capacity is released on dispatch
  completion and cleanup, with bounded response collectors. The default remains
  1024, but batch-heavy clients may now reach the intended limit sooner.
  Document worker-wide middleware limits and transport message-size/queue tuning.
- Deeply nested JSON produces `parse_error` on served connections and
  `RpcServer.handle_json()` instead of escaping as `RecursionError`. Generated
  Python clients normalize the same input to `RpcTransportError` and settle
  pending calls; regenerate clients for that decoder fix. Pending-limit and
  unknown client-method response warnings are bounded per connection, with
  aggregate counts at debug level on cleanup and unchanged observation signals.
- Generated Python and TypeScript clients ignore valid late, duplicate, and
  unknown responses without interrupting other calls, while rejecting malformed
  response envelopes. TypeScript clients answer server-to-client requests with
  `method_not_found` instead of disconnecting. Python request timeouts include
  sending; interrupted sends retire the transport and settle pending futures.
  Python clients close every owned static transport despite individual failures
  and finalize client state while preserving caller cancellation. Regenerate
  clients to pick up these fixes; public call signatures and defaults are unchanged.
- FastAPI connection middleware covers `RpcRoutes` context dependency setup
  and cleanup, including rejections. Custom handlers using FastAPI's
  `WebSocket` directly report acceptance, close codes, and connection lifetime.
  Delegating to `serve_websocket()` reuses active middleware without entering
  connection scopes twice; additional middleware also receives request, stream,
  and activity callbacks.
- Generated TypeScript stream name unions longer than one line are wrapped the
  way Prettier formats them.

## 0.10.0 - 2026-09-30

### Migration from 0.9

- `RpcService(errors=...)`, `RpcChannel.create_server(errors=...)`, and
  `RpcService.errors` are removed. Replace the global exception mapping with
  `RpcErrorBinding` declarations in the relevant operations' `raises=`.
  `error_mapper` remains available for custom mapping logic.
- Regenerate committed Python and TypeScript clients so
  `rpckit generate --check` passes and subscription failures are decoded as
  structured RPC errors.
- Normal and shutdown closes now wait until queued messages are sent, bounded by
  `RpcLimits.send_timeout`. Remove workarounds that tracked socket sends before
  calling `connection.close()`, and use `close_when_events_complete=True` for
  sockets that should end with their event sources.
- Users of earlier 0.10 PR revisions must replace `RpcErrorContract` with
  `RpcErrorBinding`, `binding.error` with `binding.error_type`, and connection
  `raises=` with `rejects=`. These names have no compatibility aliases.
- Child-channel and operation bindings override inherited bindings for the
  same exception class. Other inherited errors are kept. Conflicting bindings
  within one declaration raise `ProtocolDefinitionError` when that channel or
  operation is registered, with its name in the diagnostic.
- `rejections=` passed to `serve()`, `create_router()`, `serve_websocket()`,
  `RpcRoutes`, or `RpcTestClient` keeps working and is consulted first. Move
  mappings repeated at several call sites to bindings in
  `RpcService(rejects=...)` or in the endpoint's `socket(rejects=...)` or
  `stream(rejects=...)`.
- Child channels that only hold a single operation can become a dotted name on
  the parent channel, for example `@channel.server.method("text.insert")`. The
  wire name stays the same.

### Added

- Close finite event sockets with
  `RpcService.socket(..., close_when_events_complete=True)`. Once every event
  source of the socket has finished, the queued notifications are sent and the
  connection closes with `NORMAL`. Sockets whose channels declare no events
  reject the option.
- Bind domain exceptions to RPC errors with `RpcErrorBinding`, so domain code
  no longer imports rpckit. A binding sets the same `code`, `message`,
  `rpc_code`, and `details` as an `RpcError` subclass, reads dynamic messages
  and details from the exception, and may name the `RpcRejection` the
  exception becomes when it ends a connection. Declare it in `raises=` of
  methods, events, subscriptions, and channels, where it appears in OpenRPC and
  generated clients like an `RpcError` subclass. Lookup is scoped to each
  operation regardless of `strict_errors`; the same domain exception can have
  different bindings on different operations. The most specific binding matches
  subclasses. Required `details=Model` field names are checked at definition,
  and `binding.check(exception)` validates a representative instance. Server
  error classes use an `RpcError` suffix to avoid domain exception collisions;
  `binding.error_type` exposes the generated class. Export `RpcErrorBinding`
  and `RpcErrorDeclaration`.
- Override a channel's binding at a child channel, method, event, or
  subscription for the same domain exception. Overrides affect only that
  operation or child channel, including its OpenRPC and generated client
  declarations. Bindings for more specific exception subclasses are retained.
- Declare connection failures next to the endpoints with bindings in
  `RpcService(rejects=...)`, `RpcService.socket(..., rejects=...)`,
  `RpcService.stream(..., rejects=...)`, and `RpcRoutes(..., rejects=...)`.
  The binding's message becomes the rejection reason. A failure is looked up
  in the call's `rejections=` first (`serve()`, `create_router()`,
  `serve_websocket()`, `RpcRoutes`, `RpcTestClient`), then the endpoint's
  `rejects=`, then the service's; the first level that maps it wins.
- Pass dotted names to `method()`, `event()`, `subscription()`, `stream()`,
  `client.method()`, and `child()`. Names are relative to the channel
  namespace and each segment is validated, so
  `@channel.server.method("text.insert")` no longer needs a child channel.
  Wire names, contracts, and generated clients match the child-channel
  equivalent.
- Declare server-side operations directly on the channel:
  `@channel.method()`, `@channel.event()`, `@channel.subscription()`, and
  `@channel.stream()` are shorthands for the `channel.server` decorators.
  `channel.server` stays available for channels that also declare
  `channel.client` methods.

### Removed

- The service-wide `errors=` mapping, the channel server's `errors=` parameter,
  and the `RpcService.errors` property. No deprecated fallback is retained.
- The earlier 0.10 draft's `RpcErrorBinding.error` attribute; use `error_type`.

### Fixed

- Dishka context injection through `RpcRoutes` works on the supported minimum
  Dishka 1.7 and FastAPI 0.115 versions: the injector resolves from the
  WebSocket's container instead of requiring an HTTP request.
- Conflicting error bindings are rejected during channel or operation
  registration instead of waiting for freeze. Diagnostics identify the channel,
  method, event, or subscription and both conflicting error codes.
- Unresolvable or invalid `details=` callback annotations raise
  `ProtocolDefinitionError` with the domain exception's name and the original
  cause. Error-binding diagnostics consistently use "binding" terminology.
- `strict_errors=True` accepts subclasses of declared `RpcError` types.
- Exceptions raised by an `error_mapper` are logged and contained as
  `internal_error`, including for notifications.
- Positional JSON-RPC parameter arrays are rejected with `-32602`
  (`invalid_params`) instead of `-32600` (`invalid_request`).
- Aborted subscriptions send the declared RPC error payload instead of the
  fixed text `Subscription failed`; generated clients expose typed failures.
- `RpcConnection.close()` with `NORMAL` or `SHUTDOWN` no longer drops the
  message being sent when the close arrives. Closes wait until the queued
  messages are sent, within `RpcLimits.send_timeout` in total, so an event
  source can close right after its final `yield`.
- Cancelling a served JSON-RPC connection also cancels its reader, writer,
  event sources, and pending requests instead of leaving them running.

## 0.9.0 - 2026-09-25

### Added

- Declare the connection context of an endpoint with
  `RpcService.socket(..., context=T)` and `RpcService.stream(..., context=T)`.
  `RpcEndpoint` and `RpcStreamEndpoint` are generic in that type; it stays out
  of OpenRPC and generated clients.
- Mount endpoints individually on an existing FastAPI router with
  `rpckit.fastapi.RpcRoutes`. Its `context=` function runs as a FastAPI
  dependency per connection, path parameters included, and handlers receive the
  result as `Inject[T]`. `RpcRoutes` takes its type from the function's return
  annotation, so type checkers and `mount()` reject endpoints that declare
  another context. Routes use the endpoint's declared path.
- Integrate DI libraries with `RpcRoutes` through the `FastApiResolver`
  protocol. `rpckit.dishka.Dishka` resolves from the app's root container per
  connection and lets the context function declare `FromDishka[T]` without
  `@inject`.
- Map exceptions to connection rejections with `rejections=`, either a mapping
  from exception types to `RpcRejection` or a callable returning an
  `RpcReject`, on `serve()`, `create_router()`, `serve_websocket()`,
  `RpcRoutes`, and `RpcTestClient`. Mapped failures of `before_accept`
  hooks and `RpcRoutes` context functions reject the handshake; mapped failures
  of event sources and binary streams close the accepted socket with the
  matching code. Raising `RpcReject` behaves the same without a mapping, and
  unmapped failures keep their current behavior. Export `RpcRejections` and
  `RpcRejectionMapper`.
- Add `RpcConnectionClose.TRY_AGAIN_LATER` (WebSocket code 1013), used when an
  accepted connection is closed with `RpcRejection.UNAVAILABLE`.

## 0.8.0 - 2026-09-24

### Migration from 0.7

- Rename the Python package, CLI command, and logger from `pyrpckit` to
  `rpckit`. The PyPI distribution stays `pyrpckit`. Replace `pyrpckit` imports
  and CLI calls with `rpckit`, and update logger filters. Regenerate clients because
  generator metadata, generated headers, and the `rpckit.jsonrpc` subprotocol
  now use the new name. Old `.pyrpckit` generator manifests are no longer read.
- Regenerate and commit Python and TypeScript clients with this version's
  generator. Generated `connect()` now opens every declared server; pass
  `lazy=True` in Python or `lazy: true` in TypeScript to defer connections.
- Replace `RpcPeer` with `RpcConnectedClient` and `RpcPeerClosedError` with
  `RpcClientClosedError`. Import from `rpckit` or the documented public
  integration modules.
- Review the new 30-second request and client-method timeouts. Pass `None`
  explicitly where a call is intended to wait indefinitely.
- Handler results are validated before sending, and Pydantic validation errors
  raised inside handlers now become internal errors. Fix handler return values
  and raise declared RPC errors for expected failures.

### Added

- Give observers a `RpcObserver` base class with optional connection, notification,
  stream-frame, and slow-consumer callbacks; retain structural typing through
  `RpcObserverLike`. Request contexts now include the live connection, when
  available.
- Expose Python `client.closed` and TypeScript `onDisconnect` for unexpected
  WebSocket loss. Python and TypeScript clients can retry connections with
  bounded backoff and resume active subscriptions using `reconnect=True` or
  `reconnect: true`.
- Add parameterized `@channel.server.subscription()` streams with per-connection
  subscribe/unsubscribe lifecycle, generated Python and TypeScript iterators,
  `x-rpc-subscriptions` in OpenRPC, and `RpcLimits.max_subscriptions`.
- Bound running and waiting calls per connection with
  `RpcLimits.max_pending_requests` (default 1024). Further calls are answered
  with a `pending_limit` error instead of being queued without limit.
- Allow configured extra Python client files and exports, refresh handshake
  headers through an async factory, and accept HTTP(S) WebSocket URL overrides.
- Restore `rpckit.testing.RpcTestClient` for in-memory service tests, with
  client-method handlers, notification timeouts, and binary stream helpers.
  Add `RpcTestStream` for stream endpoints.
- Add `fastapi.serve_websocket()`, pre-accept hooks for socket and stream
  endpoints, and 401/403 handshake rejections with optional response headers.
- Validate typed socket path variables before accepting the handshake with
  `RpcService.socket(..., path_model=Model)` and inject the parsed model.
- Let binary stream handlers raise `RpcStreamClose`, map stream exceptions to
  policy-violation closes, and serialize concurrent `RpcBinaryOutput.send()` calls.
- Map domain exceptions declaratively with `RpcService(errors=...)`, optionally
  enforce method `raises=` declarations with `strict_errors=True`, and warn on
  collisions between explicitly assigned numeric RPC error codes.
- Cache endpoint protocols, warn about unknown client-method response IDs,
  simplify client-method serialization, and share request-name derivation.
- Export contracts with canonical UTF-8 JSON through `RpcContract.to_json()`
  and `RpcContract.write(path)`; CLI rendering uses the same serializer.
- Export public signature types from the package root, type service match and
  contract methods, and provide typed event and stream decorator overloads.
- Check public type completeness in CI with Pyright and guard its current
  baseline against regressions.
- Use postponed annotations for the optional Dishka and FastAPI types in
  `dishka_router` and `DishkaResolver`.
- **Breaking:** Pass handlers the exact Pydantic params class they declared,
  while retaining camel-case wire validation and serialization.
- **Breaking:** Align `RpcDisconnect(close, reason)` with connection closing,
  expose unknown peer codes as `RpcConnectionClose.OTHER` plus `raw_close_code`,
  and accept `observer=` on endpoint servers. Duplicate-channel errors name
  the colliding channels.

### Fixed

- Generated Python and TypeScript requests fail with a transport error once the
  request timeout elapses while a reconnecting client cannot reach the server,
  instead of waiting indefinitely. Subscriptions keep waiting for the reconnect.
- `rpckit generate` without the `codegen` extra and `import rpckit.fastapi`
  without the `fastapi` extra report which extra to install.
- Forward `variables` through generated `with_transports()` and
  `withTransports()` when binary streams are present, and retain the first
  notification sent immediately after a subscription is accepted.
- **Breaking:** Generated Python and TypeScript `connect()` now open all
  declared servers by default. Use `lazy=True` or `lazy: true` for on-demand
  connections; the old `eager` option is removed.
- Log unexpected RPC handler exceptions with the method name and traceback, and
  expose the original exception to response observers.
- **Breaking:** Treat Pydantic validation errors raised inside handlers as
  internal server errors. Invalid request envelopes and params retain their
  JSON-RPC error codes.
- **Breaking:** Validate handler results before sending them. Invalid results
  now produce logged internal errors instead of malformed success responses.
- Generated WebSocket clients discard notifications until a listener starts and
  drop the oldest queued notification on overflow, preserving active requests.
  Python clients can select the previous close behavior with
  `notification_overflow="close"`; TypeScript uses `notificationOverflow`.
- Close RPC socket connections and notify observers when the writer fails.
- Close slow consumer connections when queueing or sending exceeds
  `RpcLimits.send_timeout` (10 seconds by default).
- Reuse Pydantic adapters for params, results, events, and JSON-RPC envelopes;
  reuse the event codec for each event source.
- Bound JSON-RPC batches with `RpcLimits.max_batch_size` (32 by default) and
  execute batch items concurrently within `max_concurrency`.
- Isolate event source failures and skip invalid event payloads so other events
  and requests continue. `@channel.server.event(on_error="close")` restores
  connection closure for an event source.

### Changed

- **Breaking:** Default RPC request and client-method timeouts to 30 seconds.
  Configure client-method defaults with `RpcLimits.client_method_timeout`; pass
  `None` explicitly to disable a timeout for a call.
- **Breaking:** Rename `RpcPeer` to `RpcConnectedClient` and
  `RpcPeerClosedError` to `RpcClientClosedError`, so the server-side handle of
  a connection uses the same client/server vocabulary as client methods. The
  module `rpckit.peer` is now `rpckit.connected_client`.

## 0.7.0 - 2026-09-19

### Migration from 0.6

- Move server declarations to `channel.server.method`, `.event`, and `.stream`,
  and rename `channel.server()` / `endpoint.server()` to `create_server()`.
- Regenerate clients for the new client-method contract and generated layout.

### Added

- Group channel declarations by the side that implements them:
  `channel.server.method`, `channel.server.event`, and `channel.server.stream`
  for the server, `channel.client.method` for the connected client.
- Add client methods, requests the server sends to a connected client.
  `channel.client.method(name, params=..., result=..., raises=...)` declares a
  typed `RpcClientMethod`. Client method names share the name space of server
  methods, events, and streams.
- Make `RpcPeer` injectable on socket endpoints. `peer.call(client_method,
  params, timeout=...)` validates params and results, raises declared errors as
  their typed exceptions, and raises `RpcClientMethodFailedError`,
  `RpcClientMethodResultError`, `RpcClientMethodTimeoutError`, or
  `RpcPeerClosedError` otherwise. Server-originated requests use `server:<n>`
  string IDs, and outgoing calls respect `RpcLimits`.
- Describe client methods under the `x-rpc-client-methods` OpenRPC extension,
  in the same shape as `methods`.
- Generate one abstract client method class per namespace in Python clients,
  such as `RoomMediaHandler`, plus `Handler` and
  `handler_dispatcher()`. `connect(handlers=...)` registers
  handlers, which run concurrently. Declared errors of client methods gain
  `create()`.

### Changed

- **Breaking:** Remove `channel.method`, `channel.event`, and `channel.stream`.
  Declare server operations with `@channel.server.method`,
  `@channel.server.event`, and `@channel.server.stream` instead.
- **Breaking:** Rename `RpcChannel.server(...)` and `RpcEndpoint.server(...)` to
  `create_server(...)`, since `channel.server` now names the server side.
- Generated Python WebSocket transports answer incoming requests. Requests
  without a registered handler get `-32601 Method not found`, where they used to
  fail the transport.
- **Breaking:** Remove the internal in-memory test client from the distributed
  `pyrpckit.testing` module. Repository integration tests keep their transport
  helpers under `tests/`.
- Raise the generated client layout version to 10.

## 0.6.0 - 2026-09-19

### Migration from 0.5

- Regenerate binary stream clients. Open Python streams with `async with` and
  use `BinarySender` / `BinaryChannel` for input or bidirectional streams.
- Move authentication checks to the hosting adapter before `serve()` and
  configure limits and error mapping on `RpcService`.

### Added

- Add client-to-server and bidirectional binary streams. The handler signature
  determines the direction: async functions inject `RpcBinaryInput` and/or
  `RpcBinaryOutput`, and async generators remain server-to-client. The client
  ends its input with the text message `{"type":"end"}`. A disconnect before the
  end aborts the handler. Incoming frames use a bounded queue, which applies
  backpressure. `RpcInputEndMessage` and the generated `BinaryInputEnd` model
  describe the end message.
- Treat stream handler parameters without `Inject[...]` as typed path
  variables. They are validated before the handshake is accepted and rejected
  as `NOT_FOUND`.
- Add `input_content_type=` to `@channel.stream()`. Write `direction` and
  `inputContentType` to `x-rpckit-binary-streams`.
- Generate `BinarySender` and `BinaryChannel` stream clients with `send()`,
  `end_input()` / `endInput()`, and `end()` in Python and TypeScript. `end()`
  gives up after 30 seconds by default (`TimeoutError` in Python,
  `RpcStreamTimeout` in TypeScript). Error closes now raise `RpcStreamFailed`, and policy violations
  (1008) raise `RpcStreamRefused`.
- Add `send_frame()`, `end_input()`, and `closed()` to `RpcTestClient`.

### Fixed

- Prevent Starlette's normal WebSocket teardown cancellation from escaping
  FastAPI handlers as `concurrent.futures.CancelledError`.
- Keep generated Python clients `ruff check` and `ruff format` clean for
  contracts without events or without methods, servers without variables, and
  single-letter model names.
- Omit the `./models` export from generated TypeScript clients whose contract
  declares no models, so they compile.
- Ignore leading underscores when deriving error codes, so private error
  classes such as `_MissingError` produce `missing`.

### Changed

- Make generated route metadata carry its request and response types. Python
  now serializes generated Pydantic parameter models centrally in the client
  runtime, while TypeScript infers request results and notification payloads
  from the selected route instead of repeating manually supplied generics.
- Rename the generated `BinaryStreamConnection` to `BinaryReceiver`.
- Open generated Python streams only with `async with`. Stream methods now
  return an async context manager; `BinaryStreamOpening` and its `open()`
  method are gone, so an opened stream can no longer be left unclosed.
- Generated stream methods only accept the stream's own path variables, such
  as a session ID. Variables that a server also declares, such as `host`, come
  from `connect()`. The per-call `url` override is gone; to redirect a stream,
  wrap the `stream_opener` or `stream_socket_factory`. In turn, `connect()`
  only accepts server variables, not the path variables of a single stream.

- Keep authentication in the hosting framework before `serve()` instead of
  coupling it to the RPC service lifecycle. `RpcConnection` remains injectable
  in handlers for connection metadata and controlled closure.
- Configure default error mapping and runtime limits on `RpcService`, with
  optional endpoint overrides, so production routes and `RpcTestClient` share
  the same serving behavior.
- Pass mounted channels through the explicit `channels=` sequence argument.
- Add nested channels with `channel.child()`. Children inherit their parent's
  namespace, declared errors, and resolver scope, and are included when the root
  channel is mounted. Channel names may default to a dotted `namespace=`.
- Explain dotted operation-name errors with the nested-channel solution.
- Derive application error codes by stripping either `Error` or `RpcError`,
  allow `RpcInvalidParamsError(message=...)` without synthetic validation
  issues, and suggest `message=` for accidental positional strings.
- Add a service-bound `observer=` for request start, request finish, and
  connection close events with structured outcome and duration contexts.
- Expose `close_code` and `close_reason` on `RpcConnection`, including close
  information received from the peer.
- Let generated Python WebSocket clients accept `headers=`, make their
  connection object directly awaitable, and eagerly open single-server clients
  by default. Multi-server clients remain lazy unless requested otherwise.
- Accept HTTP base URLs when building contracts and translate them to WebSocket
  schemes. Add `RpcContract.to_openrpc()` and render committed JSON with Unicode
  characters intact.
- Resolve FastAPI dependencies per connection with `resolver_factory=`. Add
  `dishka_router()` to read Dishka's APP container from `app.state` and reject
  accidentally supplied SESSION containers with a targeted error.

### Removed

- Remove server-side connect hooks, `ConnectionRejected`, and the
  authentication-specific `RpcRejection.UNAUTHORIZED` and `FORBIDDEN` values.

## 0.5.0 - 2026-09-18

### Added

- Add `RpcService`, socket endpoints, connection hooks, transport-independent
  serving, and an in-memory `RpcTestClient`.
- Add receive-only binary stream definitions and endpoints, represented by the
  `x-rpckit-binary-streams` OpenRPC extension.
- Add stable string error codes and typed error details alongside numeric
  JSON-RPC codes.
- Generate concrete typed errors and namespaced stream clients for Python and
  TypeScript. Bundled WebSocket clients open stream sockets automatically.
- Export generated models, namespace classes, errors, routes, transport types,
  and stream types from each client package root.
- Add request hooks to both generated clients and async disposal support to the
  TypeScript client.
- Add `create_router()` and `FastApiSocket` as the FastAPI integration.
- Accept typed `context` values in `RpcChannel.server()` for lightweight tests,
  scripts, and custom transports without a bespoke resolver.
- Add an optional `[contract]` source and output to codegen TOML configs. A
  single `pyrpckit generate --config ...` invocation now renders the OpenRPC
  contract and every client, and `--check` verifies that complete pipeline.
- Let generated TypeScript WebSocket transports and endpoint overrides accept
  `URL` objects. Endpoint `subprotocols` are optional and inherit their contract
  default; single-server clients also accept `connect({ url })`.

### Changed

- Configure FastAPI prefixes and dependencies through `include_router()` so
  `create_router()` only exposes pyrpckit runtime options.
- Rename generated `RpcRemoteError.code` from the numeric JSON-RPC code to the
  stable string application code; the numeric value is now `rpc_code` in Python
  and `rpcCode` in TypeScript.
- Include `data.code` and `data.details` in every error envelope, including
  built-in errors whose details are `null`.
- Process requests concurrently up to `RpcLimits.max_concurrency`; responses
  may complete out of order even when requests arrive on the same connection.
- Keep FastAPI WebSocket handler signatures free of captured endpoint
  parameters and preserve unexpected WebSocket state errors.
- Make `RpcChannel` a lightweight group of methods, events, and streams; the
  channel name is positional and supplies the default namespace.
- Generate contracts from mounted services, including endpoint paths,
  variables, subprotocols, protocol version, errors, and streams.
- Derive an endpoint's default server name from the final static path segment
  (for example, `/v1/gateway` becomes `gateway`).
- Rename generated `media.py` / `media.ts` to `streams.py` / `streams.ts`.
- Rename method `errors=` to `raises=` and error extensions to
  `x-rpckit-code` / `x-rpckit-details-schema`.
- Redesign generated `connect()` around one option set, shared server variables,
  per-server overrides, injectable socket factories, and lazy sockets. Pass
  `eager=True` / `eager: true` to open every declared server in parallel.
- Place binary stream operations on their declared namespace and let their URL
  templates inherit variables supplied to `connect()`.
- Let optional Python request parameters remain unset so server-side schema
  defaults still apply.
- Treat regular binary stream closure as normal async-iteration completion and
  report it as `RpcStreamClosed` only for direct reads.
- Preserve notification pump completion so subscriptions created after a
  transport ends finish or fail immediately instead of waiting forever.

### Removed

- Remove the old app/router/module composition API, channel inclusion, and
  `RpcContract.from_channels()`.
- Remove authoring-side tags and the old FastAPI `serve()` helper.
- Remove client-to-server and bidirectional binary streams.
- Replace generated `from_transport*` / `fromTransport*` constructors with
  `with_transports()` / `withTransports()`.
- Stop exporting the generated TypeScript `ConnectOptions` and
  `<Name>Transports` types; the options are declared inline on `connect()` and
  `withTransports()`.
- Remove awaiting a Python `BinaryStreamOpening` directly; use `async with` or
  call `.open()` and close the returned connection explicitly.
