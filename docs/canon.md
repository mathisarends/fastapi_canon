# Canon package and public API

The [canonicalization record](../CANONICALIZATION_DECISIONS.md) distinguishes the
agreed target design from implemented capabilities. The prepared 0.10.0 source
adds [shared problem contracts](../packages/fastapi-canon/docs/problems.md).
RPC problem envelopes, terminal SSE control events, and the combined client
facade remain subsequent work.

This document records the implemented package boundaries and public entry points
in the prepared, unpublished Canon 0.10.0 source. The
[codegen discussion](unified-canon-concept.md) describes possible next steps;
its configuration and combined SDK examples are design candidates.

The canonical repository for development and future releases is
[`mathisarends/fastapi_canon`](https://github.com/mathisarends/fastapi_canon).
The [repository migration](repository-migration.md) preserves the imported Git
history and records the publisher cutover. Repository names do not change the
`fastapi-canon` distribution or its `fastapi_canon` imports.

## Package and dependencies

`fastapi-canon` is the only active library distribution. Its wheel contains
composition, HTTP contracts, SSE, WebSockets, WebRTC, telemetry declarations,
testing helpers and the existing generators. They share one Canon version.

Python extras select third-party dependencies, not source modules or additional
Canon distributions. The base depends on FastAPI, Pydantic and Dishka. Importing
`fastapi_canon` and displaying root CLI help must work without optional generator
dependencies, telemetry runtimes or media engines.

| Extra | Purpose |
| --- | --- |
| `sse` | FastAPI and Pydantic versions required by typed SSE |
| `websockets` | Pydantic, core-schema and typing requirements for RPC and sockets |
| `webrtc` | AnyIO, Starlette and typing requirements for session signaling |
| `aiortc` | Optional reference media engine; use with `webrtc` |
| `otel` | Native FastAPI telemetry, observed Dishka ports and WebSocket middleware |
| `codegen` | Template, schema-validation and model dependencies for generators |

The old `packages/fastapi-streams`, `packages/fastapi-rpckit` and
`packages/fastapi-webrtc` directories retain tests, examples and guides. Their
`pyproject.toml` files contain tool settings, not separate library build metadata.
The workspace and release configuration select only Canon as a library.

Dependency ranges live in `packages/fastapi-canon/pyproject.toml`;
[compatibility](compatibility.md) records the tested scope. The generated SDK's
HTTP or WebSocket client dependencies belong to its consumer. Application
providers and exporters remain application-owned.

## Public namespaces

Core composition and HTTP contracts live in `fastapi_canon`. All optional
integrations live under `fastapi_canon.integrations`: SSE, WebSockets, WebRTC
and telemetry. A namespace organizes code inside the same wheel; it does not
represent another PyPI distribution.

| Responsibility | Public imports | Guide |
| --- | --- | --- |
| Application composition and Dishka lifecycle | `fastapi_canon.Composition`, `Feature` | [Composition](../packages/fastapi-canon/README.md#feature-composition) |
| HTTP routes and contracts | `fastapi_canon.CanonRouter`, `RouterPolicy`, `Error`, `ErrorRegistry`, `CanonResponse` | [HTTP contracts](../packages/fastapi-canon/README.md#error-contracts) |
| SSE with Canon HTTP contracts | `fastapi_canon.integrations.sse.CanonStreamRouter` | [SSE contracts](../packages/fastapi-canon/docs/streams.md) |
| WebSocket RPC, messages and binary streams | `fastapi_canon.integrations.websockets.RpcChannel`, `RpcService`, `RpcRoutes`, `RpcSocket` | [WebSockets](../packages/fastapi-rpckit/docs/README.md) |
| WebRTC with Canon admission errors | `fastapi_canon.integrations.webrtc.CanonWebRtcRouter`, `Session` | [WebRTC admission](../packages/fastapi-canon/docs/webrtc.md) |
| Telemetry declarations | `fastapi_canon.integrations.opentelemetry.Telemetry`, `observe` | [Telemetry](../packages/fastapi-canon/docs/telemetry.md) |
| Isolated trace assertions | `fastapi_canon.testing.telemetry.SpanCapture` | [Telemetry testing](../packages/fastapi-canon/docs/telemetry.md) |

Typical application imports are explicit:

```python
from fastapi_canon import CanonRouter, Composition, Feature
from fastapi_canon.integrations.opentelemetry import Telemetry, observe
from fastapi_canon.integrations.sse import CanonStreamRouter
from fastapi_canon.integrations.websockets import RpcChannel, RpcRoutes, RpcService
from fastapi_canon.integrations.webrtc import CanonWebRtcRouter, Session
```

The lower-level `fastapi_canon.integrations.sse.StreamRouter` and
`fastapi_canon.integrations.webrtc.WebRtcRouter` remain available for direct FastAPI use.
Their Canon counterparts add error-registry and composition contracts. Core
imports do not eagerly load either transport. Import the aiortc adapter only
when choosing that engine.

WebSocket class names retain `Rpc` when they describe RPC or its declared socket
contracts. The namespace names the transport; it does not make every WebSocket
message an RPC method. Protocol-specific methods and lifecycles stay explicit.

## Composition and runtime ownership

Applications use feature slices and inward dependencies as described in
[architecture](../guides/architecture.md). Domain code is framework-free.
`Composition` installs features and owns the application Dishka container.
Feature routers contribute routes and declarations; dependencies own
authentication, authorization and application resources.

SSE preconditions and WebRTC admission can use Canon HTTP problems before a
response or connection is accepted. Once streaming or protocol communication
starts, failures follow that transport's declared protocol. JSON-RPC envelopes,
raw/binary sockets and `fastapi-webrtc.v1` retain their existing wire behavior.

Telemetry is explicit at the composition boundary. `observe(Port)` is declared
beside its Dishka binding. `Telemetry.rpc_middleware()` explicitly bridges the
selected providers into a WebSocket service. Optional features do not create
exporters or own application SDK providers.

## Commands and generated artifacts

There is one installed command, `canon`, also available as
`python -m fastapi_canon`. The implemented command families are:

| Command | Responsibility |
| --- | --- |
| `canon sse` | Export OpenAPI/SSE contracts and generate Python/TypeScript clients |
| `canon websockets` | Export OpenRPC/socket contracts and generate Python/TypeScript clients |
| `canon webrtc` | Export session contracts and generate TypeScript browser clients |
| `canon check errors` | Check HTTP error declarations without importing application code |

The existing emitters retain their configuration formats, runtime options and
ownership manifests. Each generator owns a distinct output directory. Generated
files are changed through generators/templates, then checked for drift.

A common configuration, orchestration command, shared model registry and combined
SDK require a separate design decision. They are discussed in the
[codegen draft](unified-canon-concept.md), and are not implemented by the command
families above. Ordinary REST generation still uses separate OpenAPI tooling.

## Documentation and release ownership

| Document | Authority |
| --- | --- |
| This document | Package boundaries, public namespaces and runtime ownership |
| Package and feature guides | Implemented signatures, options and runnable examples |
| [Migration guide](../packages/fastapi-canon/docs/package-migration.md) | Installation and import changes for existing consumers |
| [Codegen discussion](unified-canon-concept.md) | Candidate future workflow and decisions to discuss before implementation |
| [Contributing](contributing.md) | Development commands and quality gates |
| [Compatibility](compatibility.md) | Tested dependencies and consumer limits |
| [Releases](releases.md) | Prepared versions, publication gates and historical evidence |

The former transport package directories retain feature guides, examples and
separate test/tool configurations. They are not installable workspace members.
Historical changelogs and verified publication records keep their original names.
Future releases use one Canon changelog, version, tag and artifact set.
