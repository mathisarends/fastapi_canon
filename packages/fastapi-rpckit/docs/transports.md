# Transports

FastAPI is the supported application entry point for WebSocket RPC. The runtime's
`RpcWebSocket` seam remains available for isolated tests and existing adapters; it is
not a promise to maintain other framework integrations.

## Message size and transport buffering

`RpcLimits.max_message_bytes` defaults to 1 MiB and is checked after
`RpcWebSocket.receive()` returns an assembled message. Configure the transport's
message-size limit too, so oversized input is rejected before the application
buffers it. For Uvicorn with its explicit `websockets` backend:

```bash
uvicorn my_api:web --ws websockets --ws-max-size 1048576 --ws-max-queue 4
```

The Python equivalents are `uvicorn.run(..., ws="websockets",
ws_max_size=1_048_576, ws_max_queue=4)`. Use the same value in
`RpcLimits(max_message_bytes=1_048_576)`. The message limit applies to the combined
payload of a fragmented WebSocket message, not independently to each fragment.
The transport may close oversized messages with WebSocket code 1009 before fastapi_canon.integrations.websockets
can produce a JSON-RPC response.

Pin and check the backend you deploy rather than relying on `--ws auto`.
Uvicorn's `--ws-max-queue` applies to the `websockets` backend. For example, in
Uvicorn 0.35.0 `websockets-sansio` uses `ws_max_size` but does not use
`ws_max_queue`, while `wsproto` does not use either setting. Newer versions may
differ; check the [Uvicorn settings](https://uvicorn.dev/settings/) and the
backend implementation for your installed version.
The verified `websockets` mapping is in
[Uvicorn 0.35.0](https://github.com/Kludex/uvicorn/blob/0.35.0/uvicorn/protocols/websockets/websockets_impl.py).

Incoming transport queues, assembled messages, decoded JSON, and fastapi_canon.integrations.websockets's own
pending items and outgoing queue all consume memory. A message-size limit alone
does not bound their sum. The legacy `websockets` backend queues complete
messages; text can require up to four bytes per character in Python, so four
queued 1 MiB messages can take roughly 16 MiB before accounting for other buffers
or objects. See [websockets memory usage](https://websockets.readthedocs.io/en/10.0/topics/memory.html).
Tune queue lengths alongside message size and connection limits, and verify
oversized fragmented messages against the actual backend during deployment.

## FastAPI

FastAPI support is included in the base distribution:

```bash
uv add fastapi-canon[websockets]
```

Mount every endpoint declared on a service:

```python
from fastapi import FastAPI

from fastapi_canon.integrations.websockets import create_router

web = FastAPI()
web.include_router(create_router(app))
```

Common runtime options are configured once on the router:

```python
from fastapi import Depends


web.include_router(
    create_router(
        app,
        resolver=resolver,
        context={Settings: settings},
        limits=limits,
        error_mapper=map_error,
    ),
    prefix="/api",
    dependencies=[Depends(authenticate)],
)
```

When a resolver can only be built after the app exists, use
`resolver_factory=`. It receives the live `WebSocket` once per connection:

```python
web.include_router(
    create_router(
        app,
        resolver_factory=lambda websocket: Resolver(websocket.app.state.container),
    )
)
```

Configure FastAPI concerns such as the prefix and dependencies through
`include_router()`. The adapter registers JSON-RPC and binary-stream endpoints
as WebSocket routes and maps pre-acceptance rejections to HTTP denial responses
when the server supports the WebSocket denial extension. Without that extension,
the ASGI server falls back to a generic HTTP 403 denial.

FastAPI dependencies on `include_router()` are useful for transport-level
checks such as authentication. Their return values are not injected into RPC
methods; use `context=` or `resolver=` for application dependencies, or mount
endpoints individually with `RpcRoutes`.

### Mount endpoints with FastAPI dependencies

`RpcRoutes` mounts endpoints on an existing router, including routers from
other libraries. An endpoint declares the type of its connection context with
`context=`; its handlers receive that value as `Inject[T]`:

```python
from collections.abc import AsyncIterator

from fastapi_canon.integrations.websockets import Inject, RpcChannel, RpcService

output = RpcChannel("output")


@output.stream(content_type="text/plain")
async def lines(job: Inject[Job]) -> AsyncIterator[bytes]:
    async for line in job.output():
        yield line


app = RpcService()
job_events = app.socket("/jobs/{job_id}/events", channels=(jobs,), context=Job)
job_output = app.stream("/jobs/{job_id}/output", lines, context=Job)
```

`RpcRoutes` supplies that value through an async context function. It runs as a
regular FastAPI dependency per connection, path parameters included:

```python
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from fastapi_canon.integrations.websockets import RpcRoutes


async def open_job(
    job_id: UUID,
    actor: Annotated[Actor, Depends(authenticate)],
    jobs: Annotated[JobRepository, Depends(get_job_repository)],
) -> Job:
    return await jobs.get_authorized(job_id, actor)


router = APIRouter(prefix="/jobs")
job_routes = RpcRoutes(
    router,
    context=open_job,
    rejects=[job_not_found, job_access_denied],
)
job_routes.mount(job_events)
job_routes.mount(job_output)
web.include_router(router)
```

The routes take their type from the context function's return annotation, so
`job_routes` is an `RpcRoutes[Job]` and type checkers reject endpoints that
declare another context or none. `mount()` checks the same at runtime; a
context function annotated with a subclass of the declared type is accepted.
Routes without `context=` mount endpoints without a declared context. To supply
several values, declare a dataclass as the context.

`mount()` adds one WebSocket route at the endpoint's declared path, so it
always matches the contract. Mounting fails when that path lies outside the
router's prefix or the endpoint is already mounted. JSON-RPC sockets and binary
streams mount the same way.

`resolver=` accepts any fastapi_canon.integrations.websockets resolver for the remaining `Inject[T]` values.
To integrate a DI library, pass an object implementing `FastApiResolver`
instead: it creates a resolver per WebSocket and may wrap the context function.
`fastapi_canon.integrations.websockets.adapters.dishka.Dishka` is one such [integration](dependencies.md#dishka).

`rejects=` takes [error bindings](errors.md#bind-domain-exceptions) with a
`rejection=`, and `rejections=` takes a mapping or callable. Both follow
[the core rules](connections-and-events.md#map-failures-to-rejections), are
consulted before the `rejects=` of the mounted endpoint and its service, and
also cover the context function and its dependencies: before acceptance the
handshake is rejected with an HTTP denial response, after acceptance the
socket closes with the matching code. Unmapped failures propagate unchanged.
Dependencies passed to `APIRouter(dependencies=...)` run before this check, so
map their failures with FastAPI's own exception handling.

### Mount a custom connection handler

Pass `handler=` to `mount()` when an endpoint needs an application-owned
connection lifecycle, such as sending a pending status before waiting for a
resource:

```python
from fastapi import WebSocket

from fastapi_canon.integrations.websockets import Inject
from fastapi_canon.integrations.websockets.adapters.dishka import Dishka
from fastapi_canon.integrations.websockets import RpcRoutes


async def serve_job(
    websocket: WebSocket,
    job: Inject[Job],
    lifecycle: Inject[JobLifecycle],
) -> None:
    await lifecycle.run(websocket, job_id=job.id)


job_routes = RpcRoutes(router, context=open_job, resolver=Dishka())
job_routes.mount(job_events, handler=serve_job)
```

`JobLifecycle` is an application service. The handler uses the same `Inject[T]`
syntax as RPC methods: the endpoint's declared context type receives the
authorized result of `context=`, and other dependencies come from `resolver=`.
Parameter names are arbitrary. Context parameters must use `Inject[T]`; an
unmarked context or an injected subclass of the declared context type fails
during mounting. Handlers must be async functions with positional-or-keyword
or keyword-only parameters. Other parameters retain normal FastAPI dependency,
path, and query resolution.

The context dependency runs even when the handler does not request its result.
After successful dependency resolution, fastapi_canon.integrations.websockets enters one connection scope,
adds the context under the endpoint's declared type, resolves the handler's
`Inject[T]` parameters, and calls the handler. The scope opens before the
handler accepts the socket and exits on completion, failure, or cancellation.
With Dishka, injected services live in the fastapi_canon.integrations.websockets SESSION scope and can depend
on the context through `from_context`; REQUEST-scoped services belong in RPC
calls. Neither the context nor `Inject[T]` requires a Dishka `@inject` decorator.
See [Dishka integration](dependencies.md#dishka) for application setup and the
separate FastAPI dependency scope.

The route retains the endpoint's path, name, prefix validation, duplicate-mount
checks, and rejection policy. Declared failures in FastAPI dependencies,
handler injection, the handler, or connection-scope cleanup reject the
handshake before acceptance and close an accepted socket afterwards. An already
closed socket is not closed again; unmapped failures propagate.

The handler owns acceptance (including any subprotocol), messages, resource
acquisition, disconnect handling, cancellation of its tasks, and socket closure.
fastapi_canon.integrations.websockets does not start a serving loop or create runtime objects such as
`RpcConnection` for a custom handler. Standard runtime settings such as `limits=`
and `error_mapper=` are applied by the standard serving runtime, not by the
custom handler wrapper. Changing the mounting handler does not change the
exported contract; the handler must implement the endpoint's declared protocol.

### Write your own route

When a route needs its own code around the connection, such as a path that
differs from the contract, call `serve_websocket()` from a regular FastAPI
WebSocket route. It serves JSON-RPC sockets and binary streams alike, accepts
the same runtime options as `create_router()`, and returns quietly when
Starlette cancels the task after a disconnect:

```python
from fastapi import WebSocket

from fastapi_canon.integrations.websockets import serve_websocket


@web.websocket("/legacy/rpc")
async def legacy_rpc(websocket: WebSocket) -> None:
    await serve_websocket(app.endpoint("events"), websocket, context={Clock: clock})
```

Prefer `RpcRoutes` for routes that only need FastAPI dependencies.

## Custom adapters

Implement `RpcWebSocket` and pass it to `app.serve(socket)`. The protocol consists
of a handshake property and six async operations:

```python
from fastapi_canon.integrations.websockets import RpcHandshake


class MySocket:
    @property
    def handshake(self) -> RpcHandshake: ...

    async def accept(self, subprotocol: str | None = None) -> None: ...
    async def reject(self, rejection, reason: str) -> None: ...
    async def receive(self) -> str | bytes: ...
    async def send(self, message: str) -> None: ...
    async def send_bytes(self, data: bytes) -> None: ...
    async def close(self, close, reason: str) -> None: ...
```

`RpcHandshake` carries the request path, headers, query parameters, path
parameters, offered subprotocols, and optional client address. Signal client
disconnects by raising `RpcDisconnect` from `receive()` or a send operation.

`app.serve()` matches the handshake path to the declared endpoint. Pass
`root_path=` when an upstream server has already consumed a URL prefix.

For low-level dispatch tests that do not need connection behavior, obtain an
`RpcServer` from `endpoint.create_server(...)` or `channel.create_server(...)` and call
`handle()` or `handle_json()` directly.

## In-memory testing and directional faults

`fastapi_canon.integrations.websockets.testing.InMemorySocket` implements the server adapter. Its `client` view
(`InMemoryClientSocket`) provides `send()` / `recv()` / `close()` for generated
Python WebSocket clients, using the same incoming and outgoing queues. Both views
preserve close code and reason; the client exposes `close_code` and `close_reason`.
`RpcTestClient` remains the convenience client for ordinary service tests.

Each socket has independent `client_to_server` and `server_to_client`
`SocketFaults` controllers. Pass them to the constructor or configure the existing
controllers before sending frames:

```python
from fastapi_canon.integrations.websockets.testing import InMemorySocket, SocketFaults

socket = InMemorySocket(
    "/rpc",
    client_to_server=SocketFaults(drop_after=2, close_code=1011, reason="test drop"),
)
socket.server_to_client.hold()
```

- `drop_after=N` disconnects after N send attempts finish; N=0 drops before the
  first frame. Already queued frames precede the disconnect. `drop_when(frame)`
  drops before delivering a matching frame. Held frames are discarded on drop.
- `close_code=1006` (the default for injected drops) represents an abrupt
  disconnect without a close frame. It is reported as abnormal closure, never
  passed to a server adapter as a wire close code. Set `reason` to preserve a
  diagnostic or supply a regular close code such as 1008 or 1011.
- `delay=seconds` or `delay=lambda frame: seconds` delays a directional send.
  Inject `sleep=clock.sleep` to use a controllable test clock; its awaitable must
  honor cancellation. No clock-dependent test needs a wall-clock sleep.
- `hold()` buffers frames while allowing send to complete. Await `held.wait()`
  and inspect `held_count`; `release()` delivers them in insertion order.
- `half_open=True` silently discards frames in that direction without closing
  the socket. The opposite direction stays usable.
- `block()` simulates a slow consumer by blocking send itself. Await
  `blocked.wait()` to synchronize the test, then `unblock()` to release it.
  Caller cancellation propagates and does not deliver the cancelled frame.

Use one controller per connection direction. Disconnect releases blocked sends
with `RpcDisconnect`, cancels delay waits, and clears held frames. Controllers
own no background workers; temporary wait tasks are awaited during cleanup.

`InMemorySocketFactory` runs a service for each generated-client connection and
owns cleanup through an async context manager. Its `configure(socket, attempt)`
callback can script different failures for successive connections. `attempts`
includes rejected attempts, and `sockets` records connections actually created:

```python
from fastapi_canon.integrations.websockets.testing import (
    InMemorySocketFactory,
    RpcTestHandshakeRejected,
)


def configure(socket, attempt):
    socket.server_to_client.hold()


async with InMemorySocketFactory(app, configure=configure) as factory:
    factory.reject_next(status=503, reason="temporary outage")
    try:
        await factory("ws://test/rpc")
    except RpcTestHandshakeRejected as error:
        assert error.status_code == 503

    async with TasksClient.connect(socket_factory=factory) as client:
        # Start a call, await the controller's held event, then release its answer.
        ...
```

`reject_next(close_code=1008, reason="policy")` scripts a rejection with close
metadata instead of an HTTP status. Rejections raise `RpcTestHandshakeRejected`
with `status_code`, `close_code`, and `reason`. Actual endpoint handshake
rejections are preserved too. The factory forwards URL paths, headers, and
subprotocols and accepts `resolver`, `context`, and `limits` for serving. Exiting
the factory disconnects every socket and awaits all service tasks; cancellation
during connection setup also cleans up the unfinished handshake.

See the late-response regression in
[`tests/codegen/test_python_runtime.py`](../tests/codegen/test_python_runtime.py)
for a complete generated-client example using held responses.

## Measure request throughput

Measure the in-process request path over `InMemorySocket` with:

```console
uv run --locked --project ../.. --directory packages/fastapi-rpckit python scripts/bench_rpc_requests.py
```

It reports sequential round-trip latency and pipelined requests per second for
`RpcModel` and plain Pydantic params, list results, and small and 16 KiB payloads,
each the median of `--repeat` runs. Add `--profile` for the top cProfile entries
of the pipelined path. It covers frame decoding, validation, dispatch,
serialization and queueing, but excludes the ASGI server, WebSocket framing and
network. Compare runs on the same idle machine.

[Back to documentation](README.md)

# Application heartbeats

The RPC socket runtime reserves `$/ping` alongside `$/cancelRequest`. A ping
request has no params or `{}` and returns `null`. Malformed requests receive
`invalid_request`; notifications get no response. Batch responses preserve batch
shape. Control processing is bounded by frame/batch limits and the bounded
outgoing queue with its send timeout. Pings bypass request admission, handler
semaphores and request middleware, so handler overload does not make
an otherwise responsive connection appear dead.

Configure protocol ping settings in your WebSocket server separately. Those
pings let the server detect lost peers; browsers cannot originate WebSocket ping
frames, so client-side application heartbeats provide the reverse check.
## Drain before stopping an ASGI server

Trigger drain while the server still handles traffic, then stop the process.
For example, your deployment controller can call an authenticated internal
pre-stop endpoint and wait for completion before sending the termination signal:

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi_canon.integrations.websockets import RpcDrain, RpcService
from fastapi_canon.integrations.websockets import create_router

drain = RpcDrain()
rpc = RpcService(drain=drain)
# Register rpc.socket(...) and rpc.stream(...) here.


@asynccontextmanager
async def lifespan(web):
    yield
    await drain.drain(timeout=0)  # Final cleanup if pre-stop already drained.


web = FastAPI(lifespan=lifespan)
web.include_router(create_router(rpc))


@web.post("/internal/drain")  # Protect this with your deployment authentication.
async def prepare_stop():
    await drain.drain(timeout=30)
    return {"drained": True}
```

The pre-stop call must run outside an RPC handler being drained. Allow the
deployment controller at least the drain budget plus the process shutdown budget.
`--timeout-graceful-shutdown` limits Uvicorn's subsequent task shutdown; it does
not schedule this application drain. A signal sent before the pre-stop call
finishes can still interrupt work.

[Uvicorn 0.54.0 shutdown ordering](https://github.com/Kludex/uvicorn/blob/0.54.0/uvicorn/server.py#L257)
requests connection shutdown and waits/cancels tasks before sending the lifespan
shutdown event. Its [graceful shutdown settings](https://uvicorn.dev/settings/#timeouts)
therefore cannot make teardown after lifespan `yield` into a pre-close hook.
The repository's real TCP smoke test uses Uvicorn's `websockets-sansio` backend,
drains through HTTP while a WebSocket call is in flight, receives the result and
1012 close, then stops Uvicorn and verifies lifespan cleanup. This verifies the
deployment ordering beyond a lifespan-only unit test. FastAPI `RpcRoutes` and
`create_router` inherit the mounted service's coordinator automatically.
