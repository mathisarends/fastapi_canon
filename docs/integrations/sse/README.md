# Canon SSE

This guide documents the integration. The implementation
now ships in `fastapi-canon[sse]` (prepared 0.10.0, not yet published).
See the [public API](../../canon.md#public-namespaces) and
[package migration guide](../../package-migration.md).

[![CI](https://github.com/mathisarends/fastapi_canon/actions/workflows/ci.yml/badge.svg)](https://github.com/mathisarends/fastapi_canon/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](pyproject.toml)
[![MIT License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Decorate a FastAPI endpoint. Get typed streaming clients in TypeScript and Python.**

Define your events with Pydantic and add `@router.stream()`. Canon SSE
turns that definition into an OpenAPI 3.2 stream contract and generated SDKs with
named event values, typed async iteration, and shared SSE transport code.

```text
FastAPI decorator → OpenAPI 3.2 → shared HTTP IR → TypeScript + Python clients
```

## Why this exists

An endpoint labeled `text/event-stream` does not give consumers a typed SDK.
They still need to know which JSON events arrive, how to parse a fragmented
stream, and how to stop it cleanly.

OpenAPI 3.2 can describe each SSE event with `itemSchema` and its JSON payload
with `contentSchema`. Canon connects that contract to both clients,
so event models, endpoint parameters, and client types come from one definition.
A CI check catches drift between the Python app, the contract, and generated code.

## 1. Decorate your FastAPI endpoint

For the prepared 0.10.0 package, select SSE and the optional codegen extra:

```sh
uv add 'fastapi-canon[sse,codegen]'
```

Save this as `app.py`:

```python
from collections.abc import AsyncIterator
from typing import Annotated, Literal

from fastapi import FastAPI
from pydantic import BaseModel, Field

from fastapi_canon.integrations.sse import StreamRouter


class TokenDelta(BaseModel):
    type: Literal["token.delta"]
    text: str


class RunCompleted(BaseModel):
    type: Literal["run.completed"]
    run_id: str


type RunEvent = Annotated[TokenDelta | RunCompleted, Field(discriminator="type")]

router = StreamRouter(prefix="/runs", tags=["agentRuns"])


@router.stream("/{run_id}/events", operation_id="stream_run_events")
async def stream_run_events(run_id: str) -> AsyncIterator[RunEvent]:
    yield TokenDelta(type="token.delta", text="Hello")
    yield RunCompleted(type="run.completed", run_id=run_id)


app = FastAPI()
app.include_router(router, prefix="/v1")
```

The result is `GET /v1/runs/{run_id}/events`. `StreamRouter` extends `APIRouter`:
normal `Depends`, security dependencies, validation, middleware, and nested
routers keep working. FastAPI handles SSE framing, serialization, and heartbeats.
Use `methods=["POST"]` for request-body-driven streams.

## 2. Export once, generate both clients

```sh
uv run fastapi-canon sse schema \
  --app app:app --output openapi.json

uv run fastapi-canon sse generate \
  --schema openapi.json --language typescript --output generated/typescript

uv run fastapi-canon sse generate \
  --schema openapi.json --language python --output generated/python
```

These commands import the app and read its contract; no running server is needed.
The exporter validates OpenAPI 3.2 and the event schemas. Both generators consume
one HTTP intermediate representation (IR), carrying operations, parameters, and
event types into the generated SDKs.

## 3. Consume typed events

The `RunEvent` union produces `RunEventType`, with the same named values in both
languages. The discriminator narrows each event to its matching model.

**TypeScript — Fetch, `AsyncIterable<RunEvent>`, no runtime dependencies:**

```typescript
import { createClient, RunEventType } from "./generated/typescript/index.js";

const client = createClient({ baseUrl: "http://localhost:8000" });

for await (const event of client.agentRuns.streamRunEvents({ runId: "demo" })) {
  switch (event.type) {
    case RunEventType.TOKEN_DELTA:
      console.log(event.text); // TokenDelta
      break;
    case RunEventType.RUN_COMPLETED:
      console.log(event.run_id); // RunCompleted
      break;
    default: {
      const exhaustive: never = event;
      throw new Error(`Unexpected event: ${exhaustive}`);
    }
  }
}
```

**Python — HTTPX, Pydantic models, and `StrEnum` values:**

```python
import asyncio
from typing import assert_never

from generated.python import Client, RunEventType


async def main() -> None:
    async with Client("http://localhost:8000") as client:
        async for event in client.agent_runs.stream_run_events(run_id="demo"):
            match event.type:
                case RunEventType.TOKEN_DELTA:
                    print(event.text)
                case RunEventType.RUN_COMPLETED:
                    print(event.run_id)
                case _:
                    assert_never(event)


asyncio.run(main())
```

Install `httpx` and `pydantic` in a Python consumer (`uv add httpx pydantic`).
HTTPX remains the default; opt in to HTTPX2 at generation time with
`--python-http-client httpx2` and install `httpx2` instead. See
[Python HTTP library selection](clients.md#python-http-library).
Both SDKs handle fragmented SSE frames, UTF-8 decoding, HTTP errors, and resource
cleanup. TypeScript supports `AbortSignal`; Python supports task cancellation and
explicit async-generator closure. See [client usage](clients.md).

## 4. Catch contract drift in CI

Commit the exported schema and generated clients. Then check the complete chain:

```sh
uv run fastapi-canon sse check \
  --app app:app --schema openapi.json \
  --language typescript --output generated/typescript

uv run fastapi-canon sse check \
  --app app:app --schema openapi.json \
  --language python --output generated/python
```

`check` fails if the app's contract changed or generated files are stale. It
writes nothing and needs no running server. Add `--diff` to print what changed.
Pair it with TypeScript's compiler and Pyright to catch invalid parameters and
event access in consumer code.

To regenerate or check the schema and every client with one command, list them
under `[tool.fastapi-streams]` in `pyproject.toml` and run
`uv run fastapi-canon sse sync` (or `sync --check --diff` in CI); see the
[sync configuration](codegen.md#one-command-for-schema-and-clients).

| Check | What it verifies |
| --- | --- |
| Contract validation | OpenAPI structure, JSON Schemas, and local references. |
| `fastapi-canon sse check` | App → committed contract → generated client consistency. |
| TypeScript / Pyright | Parameter types, event fields, and exhaustive narrowing. |

[Codegen and CI reference →](codegen.md)

## Run the complete example

The package is developed in the [Canon repository](https://github.com/mathisarends/fastapi_canon),
which shares one pnpm lockfile for its TypeScript tooling:

```sh
git clone https://github.com/mathisarends/fastapi_canon.git
cd fastapi_canon
uv sync --locked --all-groups
pnpm install --frozen-lockfile --ignore-scripts
cd .
uv run --locked uvicorn examples.sse.agent_runs:app --reload
```

In another terminal:

```sh
# TypeScript
pnpm exec tsc -p tests/typescript/sse/tsconfig.json
node tests/typescript/sse/dist/examples/agent_runs/consume.js

# Python
uv run --locked python -m examples.sse.agent_runs.consume
```

The example includes GET and authenticated POST streams, both generated SDKs,
and committed OpenAPI regression fixtures in `tests/fixtures/sse/`. Verify all generated artifacts with
`uv run --locked python scripts/sse/codegen.py --check`.

## Documentation

See the [changelog and upgrade steps](../../history/fastapi-streams.md) when updating the library
and regenerating committed clients.

| Guide | Topics |
| --- | --- |
| [Quickstart](quickstart.md) | Setup, event models, and FastAPI integration. |
| [Contracts and codegen](codegen.md) | OpenAPI 3.2, both generators, and drift checks. |
| [Client usage](clients.md) | Enums, authentication, cancellation, and errors. |
| [Architecture](architecture.md) | Native SSE, shared IR, templates, and transports. |
| [Development](development.md) | Tests, supported scope, and roadmap. |
| [Releasing](releasing.md) | Workspace release process and package-prefixed tags. |

The current release supports GET/POST JSON SSE streams. Ordinary REST endpoints
stay in the contract for your existing REST SDK. See the
[supported scope](development.md#supported-scope-and-roadmap).

Python 3.12+ · FastAPI 0.143.3+ · Pydantic v2 · [MIT](../../history/fastapi-streams-LICENSE)
