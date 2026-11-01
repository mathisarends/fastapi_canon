# One Canon distribution

The 0.10.0 release includes composition, HTTP contracts, SSE, WebSocket
RPC/sockets, WebRTC and their client generators in one `fastapi-canon` wheel.
Earlier independent distributions remain available at their existing published
versions.

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

In 0.10.0, replace the previous `canon` executable with `fastapi-canon` in scripts
and CI. Subcommand options stay the same.

```console
uv run fastapi-canon --help
uv run fastapi-canon --version
uv run fastapi-canon check errors src --strict

uv run fastapi-canon sse schema --app app.main:app --output contracts/openapi.json
uv run fastapi-canon sse generate --schema contracts/openapi.json --language typescript --output frontend/generated/sse
uv run fastapi-canon sse sync --config pyproject.toml --check --diff

uv run fastapi-canon websockets schema app.contracts:contract --output contracts/openrpc.json
uv run fastapi-canon websockets generate --config rpcgen.toml --check

uv run fastapi-canon webrtc contract --app app.main:app --output contracts/webrtc.json
uv run fastapi-canon webrtc generate --contract contracts/webrtc.json --output frontend/generated/webrtc --check
```

`python -m fastapi_canon` accepts the same arguments as `fastapi-canon`. Each transport
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

## Common codegen workflow

Prepared 0.10.0 adds version-1 `fastapi-canon generate` configuration, shared model/error
identity and a domain SDK facade. JSON HTTP calls, SSE, JSON-RPC and sessions can
contribute to one output per language. Put generation settings in `[codegen]` in
`fastapi-canon.toml`, or `[tool.fastapi-canon.codegen]` in `pyproject.toml`. Follow the
[codegen guide](../../../docs/unified-canon-concept.md) and choose a fresh output
when adopting the common workflow. Specialized generator commands remain
available with their own configuration and ownership; never point multiple
generators at the same SDK directory.
