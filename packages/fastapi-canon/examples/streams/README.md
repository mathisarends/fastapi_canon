# Typed streams with canon errors

See [`docs/streams.md`](../../docs/streams.md) for the integration API and a
complete minimal application.

`CanonStreamRouter` combines `CanonRouter` and `fastapi_canon.integrations.sse.StreamRouter`.
It inherits the native `stream()` decorator and routes registration through
canon's existing `api_route()` compiler. Shared and operation `raises=` use the
same registry membership checks and deduplication as REST routes. `Feature`
discovers the router's registry automatically; `Composition` installs the
handlers and OpenAPI compiler.

## Installation

For an installed canon checkout or release containing this integration:

```console
pip install "fastapi-canon[sse]"
```

The prepared Canon 0.9.0 release includes the SSE implementation in the same
wheel. The `sse` extra selects FastAPI >=0.143.3 and Pydantic >=2.11.2;
base composition keeps its existing lower bounds. No separate Streams package
is installed. See the [migration guide](../../docs/package-migration.md).

## Export and generate clients

Install the locked Streams codegen dependencies with the repository's group:

```console
uv sync --locked --all-packages --all-groups
uv run --locked canon sse schema --app examples.streams.app:app --output openapi.json
uv run --locked canon sse generate --schema openapi.json --language python --output generated/python
uv run --locked canon sse generate --schema openapi.json --language typescript --output generated/typescript
```

An installed application can add `fastapi-canon[codegen]` and lock it
alongside its feature dependencies. Both clients consume the same
exported OpenAPI 3.2 document, containing typed SSE events and canon problem schemas.

## Streaming boundary

Before the response starts, canon can return RFC 9457 HTTP problem responses.
Put authentication, authorization, and resource checks in dependencies.
**All generator body code, including code before the first `yield`, executes
after the response starts.** `raises=` documents HTTP error contracts; it does
not move generator execution or catch exceptions during a stream.

Business errors during an active stream require an application-specific error
event declared in your event union. Connection failures remain transport
errors. This integration adds neither `stream_raises` nor automatic conversion
of exceptions to events.

## Regression checks

The [root CI workflow](../../../../.github/workflows/ci.yml) checks the locked workspace on Linux with
Python 3.12–3.14 and Windows with Python 3.14. Regression coverage includes
dependency problems, typed GET/POST streams, complete SSE schemas, both client
generators and Dishka lifetime/cleanup. Installed-wheel checks also exercise
Canon with Streams absent. Run the focused suite from the repository root:

```console
uv run --locked --directory packages/fastapi-canon python -m pytest tests/integrations tests/test_optional_streams.py
```
