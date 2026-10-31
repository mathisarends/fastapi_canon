# Migrate from pyrpckit to fastapi-rpckit

The successor is developed inside Canon; the original repository remains
available. Version 0.1.0 is the initial successor release.

| Before | After |
| --- | --- |
| distribution `pyrpckit` | `fastapi-rpckit` (initial version 0.1.0) |
| Python import `rpckit` | `fastapi_canon.integrations.websockets` |
| CLI `rpckit` | `fastapi-rpckit` |
| optional `[fastapi]` extra | FastAPI is required; remove the extra |
| logger `rpckit` | logger `fastapi_canon.integrations.websockets` |

Install with `uv add fastapi-canon[websockets]`; retain `[codegen]`,
`[dishka]`, and `[otel]` only where needed. Update imports throughout application
code, codegen source modules, monkeypatch targets, and configuration.

The FastAPI mounting APIs are directly available:

```python
from fastapi_canon.integrations.websockets import (
    FastApiSocket,
    RpcRoutes,
    create_router,
    serve_websocket,
)
```

The existing `fastapi_canon.integrations.websockets.adapters.fastapi` paths also remain valid.
`RpcChannel`, `RpcService`, endpoint registration, and `Inject` retain their
behavior. With Canon, put `dishka_router(service)` in a feature's routers; it
uses Canon's application container, SESSION per connection, and REQUEST per call.
See the [workspace example](../../../examples/stack_service.py).

The JSON-RPC envelopes, method names, control frames, OpenRPC `x-rpckit-*`
extensions, binary/raw framing, and `.rpcgen` manifest format are deliberately
preserved, as are the `rpckit.*` OpenTelemetry metric and attribute names so
existing dashboards continue to work. Generated clients do not require server-package imports.
Regenerating clients updates their generator attribution; use the same
`rpcgen.toml` and the renamed CLI, then run `--check` and consumer tests.

No `rpckit` Python compatibility shim is installed. The old and new distributions
can coexist during application migration because their import namespaces differ.
Existing generic adapter seams are retained for tests and migration, while the
supported product experience is FastAPI WebSockets.

For consumers still on pyrpckit 0.8 or earlier, also apply the predecessor's
0.9–0.11 changelog migrations. In particular, `rpckit.fastapi` becomes the root
FastAPI exports or `fastapi_canon.integrations.websockets.adapters.fastapi`, and `rpckit.dishka`
becomes `fastapi_canon.integrations.websockets.adapters.dishka`. Simply replacing the namespace is
insufficient for those removed modules. Observer/hooks and custom-transport
changes also require call-site updates where used.

The [workspace parity report](../../../docs/rpc-parity.md) compares against the
local pyrpckit 0.11.0 checkout and records a real Cara contract/SDK check. Cara's
full application migration is still pending. Python SDK generation now wraps
long enum defaults, event unions and route annotations for Ruff compatibility;
regenerate SDKs to receive that formatting fix.

The initial local successor name was `fastapi-wsrpc` / `fastapi_wsrpc`. PyPI
rejected its pending publisher registration as too similar to an existing project.
On 2026-10-17 the user selected `fastapi-rpckit` / `fastapi_canon.integrations.websockets`, and PyPI
accepted the new Pending Publisher for `mathisarends/canon`, `release.yml`,
environment `pypi`. Update imports, CLI commands and codegen references
if migrating from the earlier, unpublished workspace name.
