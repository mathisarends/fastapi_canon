# One Canon distribution

The prepared 0.10.0 release includes composition, HTTP contracts, SSE, WebSocket
RPC/sockets, WebRTC and their client generators in one `fastapi-canon` wheel.
It is not published yet. Earlier independent distributions remain available at
their existing published versions.

Development and future releases now use
[`mathisarends/fastapi_canon`](https://github.com/mathisarends/fastapi_canon).
The [repository migration](../../../docs/repository-migration.md) records the
source history and publishing setup. The repository move does not rename the
PyPI project or add further Python import changes.

## Installation

```console
uv add "fastapi-canon[sse,websockets,webrtc]"
uv add --dev "fastapi-canon[codegen]"
```

For the reference WebRTC engine, also select `aiortc`:

```console
uv add "fastapi-canon[webrtc,aiortc]"
```

Every install contains all Python feature modules and generator templates.
Extras install the dependency versions needed by a feature; they do not install
another Canon distribution. SSE selects FastAPI >=0.143.3 and Pydantic >=2.11.2,
while base composition retains FastAPI >=0.115 and Pydantic >=2.9. WebSockets
selects its Pydantic and typing requirements. WebRTC selects its AnyIO, Starlette
and typing requirements. `codegen` selects the template/schema tooling and model
dependencies needed to generate clients from saved contracts. It does not
require an application import or the aiortc engine.

Dishka is a base dependency. Python server WebSockets use FastAPI/Starlette;
the `websockets` Python client package is only needed by generated client
applications that select that transport. Generated SSE consumers install their
selected HTTP backend (`httpx` or `httpx2`) separately. `otel` selects FastAPI >=0.143 and OpenTelemetry API >=1.40,<2
for native HTTP telemetry, Dishka port observations and WebSocket middleware. Application
providers/exporters remain application-owned. See the [telemetry guide](telemetry.md).

Remove the old `fastapi-streams`, `fastapi-rpckit` and `fastapi-webrtc` direct
dependencies from your application. Replace Canon's former `streams` extra with
`sse`. There are no imports forwarded to the earlier distributions.

## Python imports

| Earlier import | Canon import |
| --- | --- |
| `fastapi_streams` | `fastapi_canon.integrations.sse` |
| `fastapi_rpckit` | `fastapi_canon.integrations.websockets` |
| `fastapi_webrtc` | `fastapi_canon.integrations.webrtc` |
| `fastapi_canon.integrations.streams` | `fastapi_canon.integrations.sse` (`CanonStreamRouter`) |

Public class names such as `StreamRouter`, `RpcService`, `RpcSocket`,
`WebRtcRouter`, `CanonStreamRouter` and `CanonWebRtcRouter` stay available inside
the integration namespaces. Feature `__version__` exports refer to the single
Canon version. The published `fastapi_canon.integrations.webrtc` admission-router
import remains available and now contains the complete WebRTC integration.

WebSocket framework adapters live under
`fastapi_canon.integrations.websockets.adapters`, for example `.adapters.dishka`.
Replace the former `fastapi_rpckit.integrations` prefix with that path.
Import the optional media adapter from
`fastapi_canon.integrations.webrtc.engines.aiortc`. WebSocket logger filters
should use `fastapi_canon.integrations.websockets`.

Earlier prepared snapshots used `fastapi_canon.sse`, `.websockets` and `.webrtc`.
Replace those short paths with `fastapi_canon.integrations.sse`, `.websockets`
and `.webrtc`. There are no parallel short namespace aliases. See the
[canonical API](../../../docs/canon.md) for the public namespace map.

## Commands

```console
uv run canon --help
uv run canon --version
uv run canon check errors src --strict

uv run canon sse schema --app app.main:app --output contracts/openapi.json
uv run canon sse generate --schema contracts/openapi.json --language typescript --output frontend/generated/sse
uv run canon sse sync --config pyproject.toml --check --diff

uv run canon websockets schema app.contracts:contract --output contracts/openrpc.json
uv run canon websockets generate --config rpcgen.toml --check

uv run canon webrtc contract --app app.main:app --output contracts/webrtc.json
uv run canon webrtc generate --contract contracts/webrtc.json --output frontend/generated/webrtc --check
```

`python -m fastapi_canon` accepts the same arguments as `canon`. Each transport
retains its existing command options, strict diagnostics, configuration format
and owned output directory. SSE's `[tool.fastapi-streams]` table and WebSocket
`rpcgen.toml` are retained for this first packaging step. Update command names
and application imports, then regenerate all clients to update their manifests.
Binding plugins must declare `fastapi_canon.integrations.websockets.socket_bindings` entry
points and import the Canon binding APIs.

The packaging migration retains WebRTC signaling identifiers and existing
output ownership manifests. Canon 0.10.0 adds the shared application-error
bindings and generated-client changes described in [canonical errors](problems.md).
Regenerate clients and follow that guide when adopting the new stream profile.

## Further codegen work

This release establishes one distribution and CLI. The proposed `[tool.canon]`
configuration, `canon generate` workflow, shared cross-contract type registry
and combined SDK facade are subsequent work described in the
[unified codegen concept](../../../docs/unified-canon-concept.md).
Ordinary REST generation still uses separate OpenAPI tooling. Do not configure
multiple current generators to own the same output directory.
