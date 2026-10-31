# Changelog

All notable changes to `fastapi-canon` are documented in this file.

## 0.9.0 - unreleased

### Breaking changes

- Consolidate SSE, WebSocket RPC/sockets and WebRTC into the `fastapi-canon`
  distribution. Replace dependencies on `fastapi-streams`, `fastapi-rpckit` and
  `fastapi-webrtc` with Canon's `sse`, `websockets` and `webrtc` extras. Replace
  `fastapi-canon[streams]` with `fastapi-canon[sse]`. All transport source and
  generator resources ship in the same wheel; extras select dependencies.
- Migrate imports to `fastapi_canon.integrations.sse`,
  `fastapi_canon.integrations.websockets` and `fastapi_canon.integrations.webrtc`.
  Their public exports include `CanonStreamRouter` and `CanonWebRtcRouter`.
  Update short `fastapi_canon.sse`, `.websockets` or `.webrtc` paths from earlier
  prepared snapshots. Replace the published `fastapi_canon.integrations.streams`
  path with `fastapi_canon.integrations.sse`. WebSocket framework adapters now
  live under `fastapi_canon.integrations.websockets.adapters`; update former
  `fastapi_rpckit.integrations` imports to that prefix. The former top-level
  modules and console scripts are removed. Use `canon sse`, `canon websockets`
  and `canon webrtc` for the existing generator commands.
- Regenerate clients: generator metadata now identifies Canon 0.9.0. Existing
  generated layouts, contract formats and transport behavior are retained,
  including JSON-RPC 2.0 and `fastapi-webrtc.v1`. Socket binding entry points move
  to `fastapi_canon.integrations.websockets.socket_bindings`; update binding
  package metadata.
  See the [package migration guide](docs/package-migration.md) for commands.
- Extend the public `Composition` constructor with `telemetry=`. Update wrappers
  or signature snapshots that mirror this constructor. Existing compositions
  without telemetry keep their behavior. To opt in, install `fastapi-canon[otel]`
  and pass `Telemetry(...)`; register `observe(Port)` as a Dishka Provider class
  attribute beside the binding it decorates. `Feature` requires no new metadata.

### Added

- Return unified development and future releases to `mathisarends/fastapi_canon`
  with the complete imported Git history. Update package links and publisher
  configuration; preserve historical release evidence in its original repository.
- One `canon` command and `python -m fastapi_canon` entry point, including
  `canon check errors`. Root CLI help loads no transport generators.
- Optional `codegen`, `aiortc` and `otel` dependencies in Canon.
  Native engine and generation dependencies remain absent from the base install.
- Fresh-wheel checks for the base package, each feature, codegen-only and
  combined installations, including minimum dependency profiles.
- Optional, exporter-free OpenTelemetry integration using FastAPI >=0.143 native
  telemetry and application-owned providers. Core retains FastAPI >=0.115.
- Dishka decorators for public async port methods, expected-exception events,
  optional port-duration histograms, and scope/caching/finalizer preservation.
  Port wrappers capture no argument values or exception messages/stack traces.
- Native HTTP server-span Problem Details enrichment with `canon.problem.code`;
  only 5xx set `error.type` and error status. Document the separate behavior of
  FastAPI's endpoint spans for propagated domain exceptions.
- Explicit WebSocket middleware helper forwarding application providers, isolated
  `SpanCapture` testing helpers, reference endpoint trace contracts, and an opt-in
  two-service OTLP demo. See the [telemetry guide](docs/telemetry.md).

## 0.8.1 - 2026-10-25

[GitHub release and artifacts](https://github.com/mathisarends/canon/releases/tag/fastapi-canon-v0.8.1)
and [PyPI](https://pypi.org/project/fastapi-canon/0.8.1/).

- Update the optional Streams integration and development pin to 0.5.0 for the
  TypeScript `unknownEvents` option, `--diff` drift output, the config-driven
  `fastapi-streams sync` command and faster SSE line splitting in generated
  runtimes. Canon's public API and composition behavior remain unchanged; core
  imports continue to work without Streams, RPCkit or WebRTC.
- Streams 0.5.0 changes the connect timeout of SDK-owned generated Python HTTP
  clients. Regenerate stream SDKs and follow the
  [Streams 0.5.0 upgrade notes](../fastapi-streams/CHANGELOG.md) if a client
  relied on an unbounded connect time.

## 0.8.0 - 2026-10-25

[GitHub release and artifacts](https://github.com/mathisarends/canon/releases/tag/fastapi-canon-v0.8.0)
and [PyPI](https://pypi.org/project/fastapi-canon/0.8.0/).

### Breaking changes

- Extend the public constructor signatures with `CanonRouter(policy=...)` and
  `Composition(router_policy=...)`. Update wrappers or signature snapshots that
  mirror those constructors. Existing calls without policies keep their behavior.
  To migrate a shared router helper, create a `RouterPolicy` containing its
  `error_registry`, `dependencies`, and shared `raises`, then instantiate
  `CanonRouter(policy=...)` explicitly. Keep `route_class=DishkaRoute` on the
  declaring router. Use `Composition(router_policy=...)` to extend contracts
  across included feature routers; ordinary parent `raises=` remains local.

### Added

- Export immutable `RouterPolicy` for reusable HTTP prerequisites and their error
  contracts. Combine policy, router and operation declarations with the existing
  registry collision checks and identity-based error deduplication.
- Apply composition policies to nested HTTP/SSE operations without mutating
  reusable feature routers or walking FastAPI's internal route tree. Preserve
  FastAPI dependency behavior, combine same-status Problem schemas, and reject
  manual response collisions before installing the composition.
- Optional `python -m fastapi_canon.check_error_contracts` source checker for
  explicit application exceptions missing from direct CanonRouter error
  contracts. It follows typed services, helpers and FastAPI dependencies,
  accounts for supported exception handling and inheritance, and reports
  unresolved paths as incomplete analysis. Warning output is the default;
  strict CI modes and JSON reports are opt-in.
- Reuse source-checker function summaries and inheritance results within each
  invocation, preserving error paths and incomplete-analysis diagnostics. Add a
  reproducible benchmark for source-processing and full-command timings.

### Fixed

- Mark annotated module reassignments as incomplete source analysis, preventing
  later declarations from hiding a missing route error contract or creating a
  false gap for an already registered contract.
- Report expanded `ErrorRegistry(**config)` arguments as incomplete instead of
  treating an unresolved catalog as empty and reporting missing error mappings.

## 0.7.0 - 2026-10-24

[GitHub release and artifacts](https://github.com/mathisarends/canon/releases/tag/fastapi-canon-v0.7.0)
and [PyPI](https://pypi.org/project/fastapi-canon/0.7.0/).

### Breaking changes

- Pin the optional `fastapi-canon[webrtc]` integration to WebRTC 0.3.0. Custom
  engines must use its public `StrEnum` types and keyword-only engine/channel
  options; follow the
  [WebRTC migration notes](../fastapi-webrtc/CHANGELOG.md#030---2026-10-24).
  Canon core and the Streams integration keep their existing behavior.

## 0.6.0 - 2026-10-20

### Breaking changes

- Add the optional `fastapi-canon[webrtc]` integration against WebRTC 0.2.0.
  Use `fastapi_canon.integrations.webrtc.CanonWebRtcRouter` with the existing
  `error_registry` and router/session `raises` declarations. Include failures
  from dependencies explicitly. Existing plain `WebRtcRouter` use remains valid.

### Added

- Export registered admission Problem Details in the WebRTC contract and generated
  session descriptor, using the composed registry's type URIs and exact Error
  objects. `raises` documents failures without restricting runtime mapping.
- Validate declarations against the existing registry and map dependency/factory
  failures before peer allocation. Export with `build_contract(app)` after
  composition to use the application's problem type URIs. Follow the
  [WebRTC admission guide](docs/webrtc.md) when migrating a plain router.

## 0.5.5 - 2026-10-20

- Preserve feature-router lifespans when composing application dependencies or
  feature lifespans. Routers now complete shutdown before feature resources and
  the Dishka application container are closed. Router `on_startup`/`on_shutdown`
  handlers still run exactly once, including when the app has its own lifespan.

## 0.5.4 - 2026-10-18

- Update the optional Streams integration and development pin to 0.4.0 for
  always-present SDK manifest provenance and optional error-decoder recipes.
  Canon's public API and composition behavior remain unchanged.
- Regenerate stream SDKs once and commit the updated provenance manifest before
  running drift checks; follow the
  [Streams 0.4.0 upgrade notes](../fastapi-streams/CHANGELOG.md).
- Generate absolute commit-pinned changelog links in published release notes.

## 0.5.3 - 2026-10-18

- Update the optional Streams integration and development pin to 0.3.1 for
  generated Python runtime efficiency and additive diagnostic-report provenance.
  Canon's public API and composition behavior remain unchanged; core imports
  continue to work without Streams or RPCkit.
- Regenerate stream SDKs to adopt runtime improvements; follow the
  [Streams 0.3.1 upgrade notes](../fastapi-streams/CHANGELOG.md).

## 0.5.2 - 2026-10-18

- Update the optional Streams integration and development pin to 0.3.0 and align
  installation examples. Canon's composition/runtime API is unchanged.
- Consumers regenerating Python stream SDKs must follow the stricter request
  validation and diagnostic migration in the
  [Streams 0.3.0 changelog](../fastapi-streams/CHANGELOG.md).

## 0.5.1 - 2026-10-18

- Source preparation passed workspace CI, independent wheel matrices and the
  package release dry run. See the [release process](../../docs/releases.md).

- Pin the optional Streams integration and development tooling to 0.2.0.
  Regenerated Python SDKs now enforce supported scalar constraints; follow the
  [Streams upgrade notes](../fastapi-streams/CHANGELOG.md). Canon core remains
  independent of Streams and RPCkit.

- Correct Streams integration installation examples to match the pinned dependency.

## 0.5.0 - 2026-10-17

### Added

- Public `HTTPMethod` string enum, re-exported from Python's `http` module for
  explicit method lists on REST and stream endpoints.
- Optional `CanonStreamRouter` in `fastapi_canon.integrations.streams`, combining
  native fastapi-streams GET/POST SSE generators with canon's error registry,
  router-wide and local `raises=`, and RFC 9457 HTTP problems from dependencies.
  `Feature` automatically discovers its registry; full native SSE event schemas
  are retained for Python and TypeScript client generation.
- A `streams` extra pinned to `fastapi-streams==0.1.0`, with
  independent core imports and a separate codegen development group.
- An integration API guide in `docs/streams.md` and an executable streaming
  example with Dishka-injected request services, documenting installation,
  HTTP method selection, client generation, and the boundary between
  pre-response HTTP problems, in-stream business error
  events, and transport failures.
- Workspace integration checks, both client generators, and clean-wheel core
  runtime checks without Streams. Imported upstream workflow files are historical;
  the root workflow owns CI in the monorepo.

### Changed

- Develop in `mathisarends/canon` and point package metadata to its package and
  documentation directories. Publish Streams 0.1.0 before Canon 0.5.0 because
  the optional integration pins that version.
- Consumers moving from Streams 0.0.1 must regenerate SDKs and follow its
  single-value enum and reserved error-alias migrations.

## 0.4.0 - 2026-09-10

### Added

- Added `validate_openapi_contracts(app)` and `Composition.validate(app)` as
  intention-revealing OpenAPI contract validation entry points.
- Added `install_openapi_contracts(app)` for low-level success-only
  applications. `Composition` installs the compiler automatically even when no
  error registry exists.

### Changed

- Renamed the `CanonRouter` registry parameter from `errors=` to
  `error_registry=` to distinguish the available error catalog from the
  router-level and operation-level errors declared through `raises=`.
- `Feature` now collects error registries from its direct `CanonRouter`
  contributions when `errors=` is omitted. Identical registry contributions
  across features are merged once.

### Fixed

- Made `CanonRouter` success responses self-contained so standalone JSON and
  bodyless routes no longer expose internal `x-fastapi-canon-success` metadata
  or retain FastAPI's generated JSON content.

## 0.3.0 - 2026-09-10

### Added

- Added `CanonRouter` as the canonical route declaration API, with composable
  router-level and operation-level `raises=` declarations plus a first-class
  `response=` success contract. The lower-level response compilers remain
  supported for ordinary FastAPI routers and migrations.
- Added `CanonResponse.responses()` so success-only routes can declare and
  compile response contracts without an empty `ErrorRegistry`.
- Added `ResponseConfigurationError` for FastAPI `response_model` declarations
  that conflict with a bodyless `CanonResponse`.

### Changed

- Compile response contracts from generated OpenAPI operations through the
  documented `app.openapi` hook, removing internal route-tree traversal and
  route-list copying during installation preflight.
- Validate application-level operation contracts at OpenAPI generation time.
  Plain `APIRouter` declarations no longer inspect runtime status or response
  models; use `CanonRouter` for explicit declaration checks. Hidden routes are
  outside document-level validation.

- Renamed the public success contract from `Response` to `CanonResponse` to
  avoid collisions with response classes commonly used in FastAPI routers.

### Fixed

- Bodyless non-204 success contracts now suppress FastAPI's generated JSON
  response without requiring a matching `response_class` on the route.

## 0.2.0 - 2026-09-10

### Added

- Required feature names and feature-aware composition diagnostics.
- Composition-level `router_factory` support for shared prefixes, tags,
  dependencies, and custom `APIRouter` subclasses.
- Lazy Dishka provider construction from provider classes and zero-argument
  factories.
- Reusable normalized HTTP exception contracts through
  `ErrorRegistry.responses(http_statuses=[...])`.
- Immutable success response contracts through `Response` and the `json()`,
  `empty()`, `stream()`, `binary()`, and `sse()` constructors.
- Combined success, domain-error, and normalized HTTP documentation through the
  `success=` argument to `ErrorRegistry.responses()`.

### Changed

- OpenAPI compilation replaces FastAPI's generated success media type when a
  `Response` contract is declared.
- Documentation now defines composition ownership of the generated Dishka
  container and safe handling of public Problem Details fields.

### Fixed

- Manual FastAPI response definitions without canon metadata are accepted.
- SSE, PDF, readiness, and other non-problem response contracts remain valid
  when an error registry is installed.
- Domain codes that conflict with generated `http_<status>` discriminator values
  are rejected during response compilation.

## 0.1.0 - 2026-09-10

### Added

- Immutable feature composition for FastAPI routers, Dishka providers, error
  registries, exception handlers, and lifespans.
- RFC 9457 Problem Details rendering shared by runtime handlers and OpenAPI.
- Domain, validation, HTTP exception, response-validation, and unhandled-error
  normalization.
