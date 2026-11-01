# Quickstart

[Documentation](index.md) · [Project](README.md)

## Run the example

```sh
git clone https://github.com/mathisarends/fastapi_canon.git
cd fastapi_canon
uv sync --locked --all-groups
pnpm install --frozen-lockfile --ignore-scripts
cd .
uv run --locked python scripts/sse/codegen.py
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

Both consume streamed tokens, including Unicode. The example also has a POST
stream authenticated with `Authorization: Bearer demo-token`, and a normal
`/health` REST endpoint. The example token is for local demonstration.

## Define your own stream

Canon includes Dishka, FastAPI and Pydantic; the `sse` extra selects SSE-compatible versions. Install the optional
`fastapi-canon[sse,codegen]` extra where you generate contracts and SDKs. In another
project use `uv add 'fastapi-canon[sse,codegen]'`; the workspace checkout above
already installs it.

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

`StreamRouter` extends `APIRouter`. `stream()` defaults to GET and SSE. Pass
`methods=["POST"]` for JSON request bodies. Options such as `dependencies`,
`responses`, `tags`, `operation_id` and `status_code`
pass through to FastAPI. Stable operation IDs make the generated API stable.
The original function and signature are registered directly.

Event serialization must match the exported schema. `stream()` rejects non-None
`response_model_include`, nonempty `response_model_exclude`,
`response_model_by_alias=False`, and enabled `response_model_exclude_none`,
`response_model_exclude_unset` or `response_model_exclude_defaults` at registration.
Their default/no-op values remain supported. Define a separate event model when
the wire payload needs different fields; ordinary field aliases are supported.

Async generators support `AsyncIterator[T]`, `AsyncIterable[T]` and
`AsyncGenerator[T, None]`; sync generators support `Iterator[T]`, `Iterable[T]`
and `Generator[T, None, None]`. Missing annotations, `Any`, bare containers,
coroutines returning iterators and untyped `ServerSentEvent` envelopes are
rejected. Yield application data; FastAPI owns framing, heartbeats and ASGI.

Put authentication and other HTTP preconditions in dependencies. A generator
body executes after the response starts, so exceptions inside it cannot change
the HTTP status. Represent application failures in your own event union when
needed, as the example's `RunFailed` does.
