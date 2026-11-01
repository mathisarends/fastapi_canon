# Compatibility

## Package versions

| Package | Published |
| --- | --- |
| `fastapi-canon` | 0.10.0 |
| `fastapi-streams` | 0.5.0 |
| `fastapi-rpckit` | 0.4.1 |
| `fastapi-webrtc` | 0.3.0 |

The source tree prepares **Canon 0.11.0 (unreleased)** with the same Python and
runtime dependency ranges as 0.10.0. Its telemetry API requires migration from
provider-level `observe()` to `Feature(instrumentation=[instrument(...)])`.
Regenerate Python RPC clients to receive the private dispatch-table fix; their
error exports and wire behavior are unchanged. See the
[0.11.0 migration notes](../packages/fastapi-canon/CHANGELOG.md#0110---2026-11-01)
and [release preparation](releases.md#current-versions). Source versions do not
establish publication or CI results for the prepared release.

Published versions and assets are linked in the [README](../README.md#current-releases).
Canon 0.10.0 was published on 2026-11-01. It includes the unpublished 0.9.0
consolidation; follow the [package migration guide](../packages/fastapi-canon/docs/package-migration.md)
and regenerate clients using the 0.10.0 changelog notes.
Streams 0.5.0, Canon 0.8.1 and RPCKit 0.4.1 were published on 2026-10-25; Canon
0.8.0 earlier that day; WebRTC 0.3.0 on 2026-10-24. Follow the
[Streams 0.5.0 notes](../packages/fastapi-streams/CHANGELOG.md#050---2026-10-25)
when regenerating Python stream clients, the
[Canon router-policy migration notes](../packages/fastapi-canon/CHANGELOG.md#080---2026-10-25)
when updating constructor wrappers and the
[WebRTC migration notes](../packages/fastapi-webrtc/CHANGELOG.md#030---2026-10-24)
before upgrading custom engines. The [release record](releases.md#current-versions)
links the verified source and publication workflows.
Package metadata defines dependency ranges; the root `uv.lock` selects development
versions and does not establish support for every resolvable combination.
Canon 0.10.0 combines the transport code from the unpublished 0.9.0 consolidation
in one wheel. Feature extras select third-party dependencies; no extra requires an
earlier distribution. Base composition retains its lower dependency bounds.

Canon 0.10.0 adds the [canonical error bindings and generated-client
handling](../packages/fastapi-canon/docs/problems.md). The expanded implementation
passed the current-dependency wheel matrix on Python 3.14 and the minimum matrix
on Python 3.12 on 2026-10-31: eleven independent installations per matrix, with
core Pydantic 2.9 support retained. The final release artifacts passed those
matrices again on 2026-11-01. This does not establish support for every combination
allowed by the dependency ranges.

Canon 0.8.0 adds router policies and optional source-based error-contract checking.
Its policy contracts and installed checker are exercised by the current/minimum
installed-wheel smoke matrices and public-index installation checks.

The earlier standalone releases passed exact-source CI, current/minimum wheel
matrices and installed CLI/client checks before publication. Public installs cover
Canon core, each optional extra and both extras together. A fresh Windows/Python
3.14 installation also verified all four published packages together, including
HTTP/SSE/RPC/WebRTC routing and generated RPC SDK execution and compilation.

## Verification scope

The published 0.10.0 source passed exact-commit CI on Linux/Python 3.12–3.14 and
Windows/Python 3.14, including the workspace lint, type, test and generated-client
gates. Linux/Python 3.14 also passed Chromium/aiortc browser checks. The release
wheel passed all eleven feature/tool combinations with current dependencies on
Python 3.14 and minimum direct dependencies on Python 3.12.

After upload, the publish runner lacked npm compiler dependencies. Recovery
verified the original PyPI hashes and all six public-index installation
combinations on Windows/Python 3.14, plus the combined extras on Windows/Python
3.12, before finalizing the GitHub Release from the original artifacts. See the
[release record](releases.md#current-versions) for source, CI, workflow and asset
links. Per-run manifests and artifact hashes remain in Actions and GitHub Releases.

The unified 0.9.0 source has been checked locally on Windows with Python 3.14
and current compatible dependencies, plus the minimum direct dependency matrix
on Python 3.12. Both matrices install one Canon wheel across eleven feature/tool
combinations without legacy distributions. Installed SSE CLI checks cover no
HTTP backend, HTTPX and HTTPX2; the installed WebRTC CLI generates a client that
compiles. Chromium scenarios cover the aiortc signaling and media paths. This
local validation does not replace exact-source CI or establish a publication.

Canon's optional `otel` extra requires FastAPI >=0.143 and
OpenTelemetry API >=1.40,<2. Canon core retains FastAPI >=0.115; its declarations
and disabled Dishka decorators require no OpenTelemetry imports. Recent FastAPI
versions have their own OpenTelemetry API dependency. The installed-wheel matrix
covers API-only `fastapi-canon[otel]` and the reference trace tests with an application
SDK. Runtime telemetry is exercised with the versions selected in `uv.lock`;
minimum dependencies are checked separately. See the
[telemetry guide](../packages/fastapi-canon/docs/telemetry.md) for native FastAPI
bridging, instrumentation ownership, and operation-span limits.

CI runs package-specific lint/type checks, separate Python suites and generated
client drift/TypeScript tests on Linux with Python 3.12–3.14 and Windows with
Python 3.14. Linux/Python 3.14 also runs Chromium/aiortc browser tests and installed
artifact checks. See the [active CI workflow](../.github/workflows/ci.yml).

The installed-wheel matrix covers:

| Combination | What it establishes |
| --- | --- |
| `[otel]` | Native telemetry and observed ports with the API alone, without an SDK |
| Canon base | Composition with optional tools and engines absent |
| `[sse]` | SSE runtime with the required FastAPI/Pydantic bounds |
| `[websockets]` | RPC and typed/raw/binary sockets |
| `[webrtc]` | Session declarations, signaling, admission and missing-engine diagnostics |
| `[webrtc,aiortc]` | Optional engine installation |
| `[codegen]` | Offline specialized and unified generation without transport/engine extras; execution of a generated JSON HTTP client |
| `[sse,codegen]` | SSE contracts and clients |
| `[websockets,codegen]` | OpenRPC export and client execution/compilation |
| `[sse,websockets,codegen]` | Shared application state and Dishka cleanup |
| All feature/tool extras | Combined routing and client generation |

Run `scripts/check_distributions.py` through the locked workspace environment for
current compatible dependencies. Add `--python 3.12 --dependencies minimum` for
the lowest jointly satisfiable direct runtime/extra bounds; transitive dependencies
resolve normally. Both modes build and install real artifacts in fresh environments
without local source overrides. Inventories are written to ignored
`dist/compatibility-{latest,minimum}.json`. This is a bounded smoke matrix, not
exhaustive testing of every compatible dependency combination.

The installed Streams CLI is also tested with no HTTP backend, HTTPX alone and
HTTPX2 alone. The installed WebRTC CLI regenerates the saved practice contract and
browser client, verifies provenance/drift and compiles the generated TypeScript.
See [contributing](contributing.md) for commands and [releases](releases.md) for
exact-source CI, dry-run and publication gates. Per-run results and artifact hashes
belong in Actions and GitHub Releases.

## External consumer limits

Cara's exported RPC contract can establish SDK generation/import/compilation and
drift compatibility. It cannot establish installation, mounting, authentication,
Dishka or media behavior of Cara's full application. Follow
[RPC parity and consumer checks](rpc-parity.md) for the installed-wheel command
and remaining migration scope.

For the preceding consumer integration releases, Maximus reported successful
native Linux Chromium/GStreamer integration and browser fallback tests. These
are consumer-reported results, not independent workspace deployment verification.
Deployed TURN/NAT, PostgreSQL concurrency and proxies still require
application-side validation.

The 0.10.0 unified SDK is also exercised across HTTP, SSE and JSON-RPC
with one problem/error identity and shared model classes. Tests compile and run
the generated TypeScript facade, compile its socket/WebRTC session signatures,
and exercise generated Python requests and cleanup. The common HTTP binding is
a JSON subset; multipart, other parameter styles and unsupported schemas fail
preflight. See the [codegen guide](unified-canon-concept.md) for the precise scope.
