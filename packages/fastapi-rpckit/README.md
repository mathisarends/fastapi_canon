# Canon WebSockets

This directory retains feature tests, examples and guides. The implementation
now ships in `fastapi-canon[websockets]` (prepared 0.10.0, not yet published).
See the [public API](../../docs/canon.md#public-namespaces) and
[package migration guide](../fastapi-canon/docs/package-migration.md).

**Define your realtime API once in Python. Get the server, the contract, and
typed clients for Python and TypeScript — none of which can drift apart.**

Agents, browser automation, live dashboards and voice need more than
request/response over HTTP: server-pushed events, binary streams, one
long-lived connection. So the JSON-RPC envelope gets hand-written, the dispatch
table grows by hand, and the frontend client is maintained separately — until
the two disagree in production.

fastapi_canon.integrations.websockets makes the Python definition the single source of truth:

| You write | fastapi_canon.integrations.websockets gives you |
| --- | --- |
| an async function on a channel | validated dispatch, injection, concurrency, shutdown |
| a payload model | an OpenRPC contract as a build-time artifact |
| an error class | typed exceptions in every generated client |
| an async iterator | server-pushed events and binary streams |
| nothing else | Python and TypeScript clients, regenerated in CI |

`fastapi-rpckit` is the FastAPI-first WebSocket RPC successor to
[`pyrpckit`](https://github.com/mathisarends/pyrpckit), developed in the
[Canon workspace](https://github.com/mathisarends/fastapi_canon). FastAPI is a required
dependency. This implementation now ships in Canon; SSE uses `fastapi_canon.integrations.sse`.
The existing dispatch engine, OpenRPC contracts, binary/raw channels, and client
generators are retained. The old repository remains available.

Upgrade notes for each release are in the [changelog](CHANGELOG.md); moving from
`pyrpckit` is covered by the [migration guide](docs/migration.md).

## The idea

You define each operation once, on the server:

```python
@tasks.method()
async def create(params: CreateTask, store: Inject[TaskStore]) -> Task:
    return await store.create(params.title)


@tasks.event()
async def updated(store: Inject[TaskStore]) -> AsyncIterator[TaskUpdated]:
    async for task in store.watch():
        yield TaskUpdated(task=task)
```

One command turns that into an OpenRPC document and clients in both languages:

```bash
canon websockets generate --config rpcgen.toml
```

And your frontend gets the whole API fully typed — no schema written twice, no
client kept in sync by hand, no stringly-typed method names:

```ts
const task = await client.tasks.create({ title: "Ship 0.6" }); // Task

for await (const update of client.tasks.updated()) {           // TaskUpdated
  render(update.task);
}
```

Run `--check` in CI and a definition that outgrew its clients fails the build
instead of shipping.

## Install

```bash
uv add fastapi-canon[websockets]
uv add "fastapi-canon[websockets,codegen]"  # client generation
uv add "fastapi-canon[websockets,otel]"     # OpenTelemetry integration
```

Python 3.12–3.14. FastAPI, Pydantic and Dishka are Canon base dependencies.
Code generation and OpenTelemetry dependencies remain optional extras.

## Serve with FastAPI

```python
from fastapi import FastAPI
from fastapi_canon.integrations.websockets import RpcChannel, RpcService, create_router

greetings = RpcChannel("greetings")


@greetings.method("hello")
async def hello() -> str:
    return "Hello"


rpc = RpcService()
rpc.socket("/rpc", channels=(greetings,))
app = FastAPI()
app.include_router(create_router(rpc))
```

For feature-level mounting on an existing `APIRouter`, use `RpcRoutes`. For
Canon/Dishka composition, see the [three-transport example](../../examples/stack_service.py).
Both reuse the same WebSocket runtime; no second RPC router model is introduced.

## Quickstart

Channels group related operations and provide their namespace; a service mounts
them on a socket. Their dispatch logic can also be called directly:

```python
from fastapi_canon.integrations.websockets import (
    Inject,
    RpcChannel,
    RpcModel,
    RpcService,
    RpcSuccess,
)


class CreateTask(RpcModel):
    title: str


class Task(RpcModel):
    id: int
    title: str


class TaskStore:
    def __init__(self) -> None:
        self._tasks: list[Task] = []

    async def create(self, title: str) -> Task:
        task = Task(id=len(self._tasks) + 1, title=title)
        self._tasks.append(task)
        return task


tasks = RpcChannel("tasks")


@tasks.method()
async def create(params: CreateTask, store: Inject[TaskStore]) -> Task:
    """Create a task."""
    return await store.create(params.title)


app = RpcService(version=1)
app.socket("/rpc", channels=(tasks,))


async def test_create() -> None:
    server = tasks.create_server(context=TaskStore())
    response = await server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tasks.create",
            "params": {"title": "Ship 0.6"},
        }
    )
    assert isinstance(response, RpcSuccess)
    assert response.result == Task(id=1, title="Ship 0.6")
```

`tasks.create` is the wire name, the docstring becomes the contract summary,
and `Inject[TaskStore]` is resolved on the server — it never appears in the
public schema.

Every operation on a channel is implemented by the server: `@tasks.method()`,
`@tasks.event()`, `@tasks.subscription()`, and `@tasks.stream()` declare the
whole API. Clients call methods on the server; the server never calls methods
on the client. Push data to clients with events or subscriptions instead.

## Core API

Most services need only these names; everything else in `fastapi_canon.integrations.websockets` is for
hooks, limits and custom transports.

| Name | Use it for |
| --- | --- |
| `RpcChannel` | Group operations under a namespace with `@channel.method()`, `.event()`, `.subscription()` and `.stream()`. |
| `RpcModel` | Base class for parameter, result and event payloads. |
| `RpcService` | Mount channels on a socket path with `service.socket(...)`. |
| `create_router` / `RpcRoutes` | Serve a service from a FastAPI app or an existing `APIRouter`. |
| `Inject[T]` | Resolve a server-side dependency; it never appears in the contract. |
| `RpcError` | Subclass for typed errors; declare them with `raises=` so generated clients raise matching exceptions. |

Reach for `RpcLimits`, `RpcMiddleware` and the connection hooks when you need
quotas, observability or handshake checks; see the guides below.

## Documentation

Read the [hardening stack and SDK migration guide](docs/hardening-overview.md) for all
15 ticket PRs, changed defaults, and integration steps.

- [Services and channels](docs/services.md) — methods, namespaces, parameter
  styles, sockets, protocol versions
- [Dependency injection](docs/dependencies.md) — `Inject[T]`, resolvers,
  scopes, Dishka
- [Connections and events](docs/connections-and-events.md) — the `connect`
  hook, rejecting handshakes, server-pushed events, limits
- [Typed errors](docs/errors.md) — stable codes, typed details, generated
  exception classes
- [Binary streams](docs/streams.md) — upload, download, and bidirectional byte streams beside JSON-RPC
- [Sockets](docs/sockets.md) — proxy native protocols such as CDP with typed protocol bindings
- [Contract and clients](docs/clients.md) — `rpcgen.toml`, the CLI, the shape
  of generated clients
- [Transports](docs/transports.md) — FastAPI, custom sockets, testing

## Examples

[`examples/`](examples) holds standalone runnable scripts, and
[`examples/generated_clients`](examples/generated_clients) contains real
generated Python and TypeScript output you can read before installing
anything.

## Development

Run from the [Canon workspace](https://github.com/mathisarends/fastapi_canon) root with
its single lockfile:

```bash
uv sync --locked --all-packages --all-groups
uv run --locked python scripts/check.py lint
uv run --locked --project ../.. --directory packages/fastapi-rpckit python -m pytest
```
