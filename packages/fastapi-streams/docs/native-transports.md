# Native SSE transports and mixed client generation

An authoritative OpenAPI 3.2 contract can describe an existing SSE endpoint.
`canon sse generate --schema` does not require `StreamRouter`, a running
server or replacement of the application's stream implementation.

The executable [native proxy example](../examples/native_proxy.py) declares an
explicit event union through `itemSchema.properties.data.contentSchema` and
registers its referenced models. Its `StreamingResponse` forwards upstream raw
bytes, status and selected response headers. Keepalives and unknown event frames
are not decoded or reserialized. The response closes the upstream connection;
the FastAPI lifespan owns the HTTP client. Application authentication, allowed
headers, run identity, recovery and replay remain application responsibilities.
The declared 200 contract must match the reviewed upstream; other upstream status
codes are preserved and generated SDKs apply their usual HTTP error handling.

From this package directory:

```console
uv run canon sse schema --app examples.native_proxy:app --output openapi.json
uv run canon sse generate --schema openapi.json --language python --python-http-client httpx2 --output generated/sse
uv run canon sse check --schema openapi.json --language python --python-http-client httpx2 --output generated/sse
```

Keep `openapi.json` as the single authoritative artifact for REST and SSE. If an
existing REST generator accepts only OpenAPI 3.1, derive a private codegen view:

```console
uv run python -m examples.rest_codegen openapi.json build/rest.openapi.json
uv run python -m examples.rest_codegen openapi.json build/rest.openapi.json --check
```

The [adaptation example](../examples/rest_codegen.py) deep-copies the contract,
sets the private view to 3.1 and replaces each inline JSON SSE `itemSchema` with
a media `schema` referencing decoded event data. This keeps event models reachable
for REST generators that use reachability or schema-tag filtering. That schema
is a private model-discovery aid, not the SSE wire body. Filter SSE operations out
of ordinary HTTP method generation and configure your generator's model/tag
selection explicitly. All components and REST operations remain available.

This recipe handles ordinary REST operations and inline JSON SSE envelopes. It is
not a general OpenAPI 3.2 downgrade: review additional 3.2 features against the
chosen REST generator. Unsupported SSE envelope shapes fail explicitly. Never
publish the private view as the service contract or edit it by hand. Generate it
from the authoritative artifact before REST generation/drift checks; generate
and check the SSE SDK directly from the authoritative artifact. Keep HTTP and
SSE outputs in separate directories. Run both clients' consumer tests.

[Back to code generation](codegen.md)
