# fastapi-canon

See [canonicalization decisions](CANONICALIZATION_DECISIONS.md) for the agreed
target design and [shared problem contracts](packages/fastapi-canon/docs/problems.md)
for the implemented HTTP, SSE, RPC, and generated-client error workflow in the
prepared 0.10.0 source.

One workspace for the FastAPI application stack: architecture conventions,
feature composition and HTTP contracts, typed SSE, WebSocket RPC, and WebRTC
sessions.

The prepared Canon 0.10.0 source consolidates the stack into one `fastapi-canon`
distribution. SSE, WebSockets and WebRTC are bundled integrations in that same
package. Extras select their third-party dependencies; they do not install the
earlier Canon distributions:

```console
uv add "fastapi-canon[sse,websockets,webrtc]"
uv add --dev "fastapi-canon[codegen]"
uv run canon --help
```

These changes are not published yet. See the
[migration guide](packages/fastapi-canon/docs/package-migration.md) for imports,
commands and the current codegen scope.

The [Canon architecture and public API](docs/canon.md) defines the shared
namespaces and ownership rules. The [codegen discussion](docs/unified-canon-concept.md)
collects candidate next steps for review before implementation.

## Repository

The canonical home for development, issues and future releases is
[`mathisarends/fastapi_canon`](https://github.com/mathisarends/fastapi_canon).
The consolidated source returns from `mathisarends/canon` with its complete Git
history. The PyPI project remains `fastapi-canon`, and Python imports remain
`fastapi_canon`. There is one maintained source repository, not a mirrored pair.
See the [repository migration](docs/repository-migration.md) for the cutover
sequence, publisher setup and historical release locations.

## Current releases

The verified publications below belong to the earlier four-distribution layout.
The prepared consolidation publishes only `fastapi-canon`; the other names
remain historical releases. The unified source is not yet on PyPI.

| Package | Version | GitHub | PyPI |
| --- | --- | --- | --- |
| `fastapi-canon` | 0.8.1 | [Release notes and artifacts](https://github.com/mathisarends/canon/releases/tag/fastapi-canon-v0.8.1) | [Install](https://pypi.org/project/fastapi-canon/0.8.1/) |
| `fastapi-streams` | 0.5.0 | [Release notes and artifacts](https://github.com/mathisarends/canon/releases/tag/fastapi-streams-v0.5.0) | [Install](https://pypi.org/project/fastapi-streams/0.5.0/) |
| `fastapi-rpckit` | 0.4.1 | [Release notes and artifacts](https://github.com/mathisarends/canon/releases/tag/fastapi-rpckit-v0.4.1) | [Install](https://pypi.org/project/fastapi-rpckit/0.4.1/) |
| `fastapi-webrtc` | 0.3.0 | [Release notes and artifacts](https://github.com/mathisarends/canon/releases/tag/fastapi-webrtc-v0.3.0) | [Install](https://pypi.org/project/fastapi-webrtc/0.3.0/) |

The table records verified publications in the former
[`canon` release list](https://github.com/mathisarends/canon/releases).
Future releases use this repository's
[release list](https://github.com/mathisarends/fastapi_canon/releases), one Canon
version, tag, changelog and artifact set. Historical release URLs and assets stay
at their original locations.

## Workspace

| Component | Location | Distribution / import |
| --- | --- | --- |
| Composition, Dishka, HTTP contracts | [`packages/fastapi-canon`](packages/fastapi-canon) | `fastapi-canon` / `fastapi_canon` |
| Typed SSE and generated clients | [`SSE module`](packages/fastapi-canon/fastapi_canon/integrations/sse), [`tests and guides`](packages/fastapi-streams) | `fastapi-canon[sse]` / `fastapi_canon.integrations.sse` |
| WebSocket RPC and generated clients | [`WebSockets module`](packages/fastapi-canon/fastapi_canon/integrations/websockets), [`tests and guides`](packages/fastapi-rpckit) | `fastapi-canon[websockets]` / `fastapi_canon.integrations.websockets` |
| WebRTC sessions and generated browser clients | [`WebRTC module`](packages/fastapi-canon/fastapi_canon/integrations/webrtc), [`tests and guides`](packages/fastapi-webrtc) | `fastapi-canon[webrtc]` / `fastapi_canon.integrations.webrtc` |
| Architecture conventions | [`guides`](guides) | feature slices, inward dependencies, Dishka |
| Runnable architecture example | [`examples/reference-service`](examples/reference-service) | development-only workspace member |

`fastapi-rpckit` is the FastAPI-first successor to `pyrpckit`, with its existing
WebSocket engine and protocol retained. See the [RPC migration guide](packages/fastapi-rpckit/docs/migration.md).
Its initial release is 0.1.0. The existing source repositories remain available;
current development and future Canon releases live here.

Start with [architecture](guides/architecture.md) when building an application.
Use the [canonical namespace map](docs/canon.md#public-namespaces) to choose public
imports and the feature guides for their implemented options. The
[stack example](examples/stack_service.py) combines HTTP, SSE and RPC using one
Dishka application container.

Development uses a `uv` workspace with one root lockfile and separate
test processes for each member. See [contributing](docs/contributing.md) for
commands, [compatibility](docs/compatibility.md) for the tested scope, and
[releases](docs/releases.md) for publication checks and historical releases.

The imported repositories remain available in Git history. Current development
and future releases use `mathisarends/fastapi_canon`; historical releases and
predecessor repositories are preserved.
