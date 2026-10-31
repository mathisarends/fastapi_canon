# Architecture

[Documentation](README.md) · [Project](../README.md)

The library follows one path from a Python definition to a typed client:

```text
Pydantic events + StreamRouter
              ↓
Native FastAPI OpenAPI → validated OpenAPI 3.2
              ↓
HTTP contract IR + enum normalization
              ↓
TypeScript / Python templates → deterministic generated files
              ↓
Fetch / HTTPX transport → typed asynchronous iteration
```

## Server integration

[`StreamRouter`](../../fastapi-canon/fastapi_canon/integrations/sse/router.py) extends `APIRouter`. Registration
checks the generator and its event annotation, then registers the original
function with `response_class=EventSourceResponse`. FastAPI handles dependency
resolution, request validation, event serialization, heartbeats and ASGI
response handling. Normal router inclusion and dependency overrides remain
native FastAPI behavior.

Checks that must produce an HTTP error belong in dependencies. A generator's
body runs after streaming starts. Application failures can be modeled as event
variants; the library does not impose an error envelope.

## Contracts

[`openapi.py`](../../fastapi-canon/fastapi_canon/integrations/sse/codegen/openapi.py) copies the app's contract,
applies the OpenAPI version/dialect adjustments, and validates its structure,
Schema Objects and local references. The official structural schema and its
license are bundled for offline validation. Ordinary REST operations stay in
the exported document.

The client event type comes from the SSE envelope's
`itemSchema.properties.data.contentSchema`, describing the decoded JSON data
rather than the HTTP response as one JSON value.

## Shared intermediate representation

[`ir.py`](../../fastapi-canon/fastapi_canon/integrations/sse/codegen/ir.py) reads the contract into HTTP-specific
operations, parameter locations, bodies, success statuses, error responses,
security metadata and type declarations. Only models reachable from streaming
operations enter client generation.

[`enums.py`](../../fastapi-canon/fastapi_canon/integrations/sse/codegen/enums.py) derives named string enums for
discriminators and literal values. Overlapping discriminated unions share enum
identity so the Python client's narrowing remains consistent.

Both language generators consume this IR. Neither generates code directly while
traversing raw OpenAPI dictionaries. There is no RPC transport or dependency on
`pyrpckit`.

## Code generation and file ownership

The [TypeScript](../../fastapi-canon/fastapi_canon/integrations/sse/codegen/typescript.py) and
[Python](../../fastapi-canon/fastapi_canon/integrations/sse/codegen/python.py) renderers produce declarations,
operation groups and client entry points. Jinja templates keep operation code
separate from contract processing; shared runtime files contain transport logic.

[`writer.py`](../../fastapi-canon/fastapi_canon/integrations/sse/codegen/writer.py) compares exact UTF-8 output,
writes files atomically and records owned paths in `.fastapi-streams.json`.
Regeneration removes stale owned files. Check mode only reads. Paths are checked
before writing, including stale manifest entries.

## Client transports

TypeScript uses Fetch, `ReadableStream`, incremental UTF-8 decoding and a shared
SSE parser. Python uses HTTPX streaming responses and validates event data with
generated Pydantic models. Both handle fragmented frames, comments, multiline
data, EOF and cleanup. Each distinguishes HTTP errors, malformed event data and
transport failures.

TypeScript returns `AsyncIterable<T>`. Python returns a typed async generator;
use `contextlib.aclosing` when breaking early. See [client usage](clients.md)
for ownership and cancellation examples.

## Validation

The test suites cover native routing and dependencies, exported contracts,
strict client types, recursive models, parser fragmentation and cleanup. Real
HTTP acceptance tests run the FastAPI example and consume it with both generated
clients. Package smoke tests install the built wheel with and without the
codegen extra. [Development](development.md) lists the local and CI commands.
