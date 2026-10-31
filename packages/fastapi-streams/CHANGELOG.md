# Changelog

All notable changes and consumer upgrade steps are recorded here. Entries marked
unreleased describe prepared source versions that have not been published.

## 0.5.0 - 2026-10-25

[GitHub release and artifacts](https://github.com/mathisarends/canon/releases/tag/fastapi-streams-v0.5.0)
and [PyPI](https://pypi.org/project/fastapi-streams/0.5.0/).

### Breaking changes

- The client that a generated Python SDK creates when no `http_client` is
  passed now uses `httpx.Timeout(None, connect=10.0)` (also exported as
  `runtime.DEFAULT_TIMEOUT`) instead of `timeout=None`. Connecting to an
  unreachable host now fails with `TransportError` after 10 seconds instead of
  hanging; read, write and pool waits remain unbounded, so idle streams are
  unaffected. This changed default requires a minor release under the pre-1.0
  version policy. Migration: regenerate the SDK. To keep unbounded connects,
  pass `RequestOptions(timeout=None)` per request or inject your own client,
  for example `Client(url, http_client=httpx.AsyncClient(timeout=None))`.
  Injected clients and explicit `RequestOptions.timeout` values behave as
  before.

### Added

- Generated TypeScript clients accept `RequestOptions.unknownEvents`. The default
  `"forward"` keeps the current behavior; `"ignore"` skips JSON objects whose
  discriminator is a string tag the generated event union does not declare, so
  exhaustive `switch` statements survive additive server variants. Each
  generated operation now embeds its discriminator and known tags. Operations
  without a top-level discriminated union reject `"ignore"` with a `TypeError`
  before any request. Regenerate TypeScript SDKs to adopt the option.
- `--diff` for `schema --check`, `generate --check`, `check` and `sync --check`
  prints a unified diff of each out-of-date file (including new and stale owned
  files and a changed application contract) to stdout, so CI logs and agents can
  see the drift without regenerating locally.
- `fastapi-streams sync` reads `[tool.fastapi-streams]` (`app`, `schema` and
  `[[tool.fastapi-streams.clients]]` with `language`, `output` and the existing
  generation options) from `pyproject.toml` or `--config`, exports the schema and
  regenerates every client. `sync --check` reports all stale targets read-only.
  Unknown keys and invalid values are rejected with exit code 2.
- `fastapi_streams.codegen.writer.planned_files` returns the files including the
  ownership manifest that `write_files` would write.

### Performance

- Generated Python and TypeScript runtimes split each decoded SSE chunk at line
  endings instead of iterating characters. With the repository benchmark,
  framing nested events is about 17x faster in Python and 4x faster in
  TypeScript; complete small/nested stream consumption is 2-4.7x faster. Long
  lines spanning many chunks are joined once. Framing semantics are unchanged:
  LF/CR/CRLF (also split across chunks), the leading BOM, multi-line `data`,
  comments and the end-of-stream frame behave as before. A randomized test
  compares both runtimes with the previous character parser. Regenerate SDKs to
  adopt the faster runtime.

### Documentation

- Point the README badge, example checkout and quickstart to the Canon workspace
  repository and its locked workspace commands. The documentation check now
  rejects links to the retired standalone repository.
- Expand the package `AGENTS.md` with the module layout, focused test/codegen
  commands and the template-regeneration workflow.

## 0.4.0 - 2026-10-18

### Breaking changes

- CLI drift checks now include SDK provenance in `.fastapi-streams.json`.
  Existing files-only manifests, a different generator version, or a changed
  source schema now fail `check`, including source changes that leave generated
  code identical. Regenerate once with the existing language and HTTP backend,
  review and commit the updated manifest. This stricter drift check requires
  a minor release under the pre-1.0 version policy.

### Added

- Always record generator/version, canonical schema SHA-256, target and Python
  HTTP backend in CLI-generated manifests, sharing metadata with diagnostic
  reports. Preserve `files` ownership, safe cleanup and read-only checks.
  Low-level file writers can supply optional `generation` metadata.
- Document optional operation/status-specific error decoders using the existing
  TypeScript guard hook and cached Python TypeAdapters. Add a checked consumer
  decoder example; unexpected JSON/text/proxy errors retain the generic fallback.
  Generated request/event APIs, HTTP/SSE contracts and runtime dependencies stay
  unchanged.

### Upgrade from 0.3.1

1. Upgrade the generator to 0.4.0 and regenerate with the same language and
   Python HTTP backend. Commit `.fastapi-streams.json` with the generated SDK.
2. Expect a one-time manifest diff. CI checks now detect schema or generator
   provenance changes even when generated source code is otherwise unchanged.
3. Existing callers, HTTP/SSE contracts and exact diagnostic allowances require
   no migration. Optional error decoders remain consumer-controlled.

## 0.3.1 - 2026-10-18

### Added

- Repeatable Python and Node benchmarks for generated SDK request preparation,
  decoding/validation, SSE framing, complete consumption, fragmented chunks and
  Python unknown-event ignoring. Reports include individual timing samples and
  optional separate Python peak-memory measurements; no CI timing thresholds.
- Add generation provenance and validation capabilities to version 1 diagnostic
  reports: canonical schema SHA-256, generator version, target/HTTP backend and
  request/event/error validation phases. Existing diagnostic fields, IDs and
  exact reviewed allowances remain unchanged. Additional report object keys are
  additive; the metadata does not introduce runtime checks or full JSON Schema
  guarantees.

### Performance

- Reuse the JSON-value adapter for skipped unknown events and HTTP-error JSON
  decoding. Cache top-level discriminator metadata per generated event adapter
  instead of rebuilding its JSON Schema for every ignore-policy stream.
- Prepare sanitized validation details only for errors that are actually raised.
  Known events still use direct Pydantic JSON validation; malformed known events,
  missing/non-string tags, invalid JSON and unknown nested variants still fail.
  Public signatures, coercion, validation timing and wire encoding are unchanged.

### Upgrade from 0.3.0

1. Upgrade the generator to 0.3.1 and regenerate with the existing HTTP backend
   to obtain the Python runtime improvements. Existing generated SDKs keep working.
2. Diagnostic-report readers should tolerate the new `generation` and `validation`
   keys. Existing exact diagnostic allowances need no migration.
3. Continue validating server requests and domain invariants at their own
   boundaries; consumers should reuse parsed events instead of validating the
   same schema again. TypeScript remains static-only with no added dependency.

## 0.3.0 - 2026-10-18

### Breaking changes

- Regenerated Python SDKs validate path, query and header parameters at the
  method call, before serialization and HTTP I/O. Invalid types or supported
  scalar constraints now raise Pydantic `ValidationError` locally. Regenerate
  SDKs, fix rejected arguments, and catch request validation around the method
  call rather than only around stream iteration. Request bodies retain their
  existing validation timing; HTTP still starts when iteration begins.
- TypeScript generation now diagnoses scalar constraints it cannot enforce.
  Review the selected target's diagnostics before promoting warnings to errors.
  Existing known limitations can be accepted by exact diagnostic ID with a
  review reason; do not suppress the warning category globally.

### Added

- Cache generated parameter, body and event TypeAdapters. Keep valid parameter
  encoding, integer representations of number parameters, query array explosion,
  optional omission and JSON body aliases unchanged.
- Source-located diagnostics include JSON Pointer, operation ID, method/path,
  keyword/value, target and enforcement limitation. Referenced and inline schemas
  are located in the original document, with each affected operation identified.
- Add `--diagnostics FILE`, `--strict` and `--allow-diagnostics FILE` to generation
  and drift checks. Reports are deterministic JSON with stable identities;
  malformed, duplicate and stale allowances fail before generated files change.
  Repository drift checks use strict mode with reviewed TypeScript limitations.
- Generated Python `StreamParseError` exposes `operation_id`, `category`
  (`invalid_json` or `invalid_event_schema`) and `validation_errors`. Structured
  details exclude input values and validator context. Keep `.data`, exception
  chaining and the existing constructor; raw data and chained causes can contain
  sensitive values and are never logged automatically by the SDK.
- Add request/diagnostic/error regression tests for both Python HTTP backends and
  installed-wheel consumers, plus a documented validation support matrix.

### Upgrade from 0.2.0

1. Upgrade the generator to 0.3.0 and regenerate with your existing HTTP backend.
2. Validate consumer call sites, including invalid requests, before adoption.
   Ordinary Pydantic coercion applies; valid numeric serialization is preserved.
3. Generate JSON diagnostics and review exact allowances before enabling strict
   CI. Changed locations, constraints, values, operations or targets need review.
4. Read stream failure categories and structured details through public fields.
   Keep raw payloads and full exception chains out of routine logs.

## 0.2.0 - 2026-10-18

- Source preparation passed workspace CI, independent wheel matrices and the
  package release dry run. See the [release process](../../docs/releases.md).

### Added

- Preserve supported string patterns/lengths and numeric bounds/multiples in the
  codegen IR and generated Python Pydantic annotations, including nested and
  referenced scalars. Diagnose unsupported constraints and regex expressions.
- Add `RequestOptions(unknown_events="ignore")` to generated Python SDKs. It
  skips unknown string tags in a top-level discriminated event union; the default
  remains an error. Malformed known events, missing tags and invalid JSON fail.
  Both HTTPX backends retain their validation and transport behavior.
- Add tested contract-first native proxy and private OpenAPI 3.1 REST-codegen
  recipes. The authoritative OpenAPI 3.2 artifact remains unchanged.

### Breaking generated-code changes

- Regenerated Python models enforce scalar rules that 0.1.0 silently dropped.
  Fix invalid payloads before adopting the new SDK. Unsupported constraints emit
  `UnsupportedConstraintWarning`; constraints alongside enum/const/composition
  and unsupported Python regex patterns fail explicitly. Move supported scalar
  constraints onto scalar branches where possible. This is not full JSON Schema
  validation. TypeScript remains static-only.
- Regenerate committed SDKs and run consumer validation tests. Pass HTTPX2
  generation/check options consistently if used. No wire names change.

### Upgrade from 0.1.0

1. Update `fastapi-streams[codegen]` to 0.2.0 and regenerate both SDKs from the
   authoritative OpenAPI document. Keep the selected Python HTTP backend on
   generation and check commands.
2. Fix data violating preserved scalar constraints and review all unsupported-rule
   diagnostics. Re-run consuming client validation and stream tests.
3. Keep the default unknown-event error policy, or opt in per request only where
   the upstream intentionally emits additional top-level discriminator values.

### Release verification

- Extend installed-wheel HTTPX/HTTPX2 smoke consumers to verify scalar constraints,
  default unknown-event rejection, opt-in skipping and malformed known events.

### Documentation

- Fix PyPI installation instructions and supported-scope wording. Replace the
  predecessor release guide with the workspace release process.

## 0.1.0 - 2026-10-17

### Added

- Generated TypeScript JSDoc and Python method docstrings include OpenAPI
  summaries, descriptions, deprecation, and SDK-specific iteration, cancellation,
  validation, and error behavior. Endpoint descriptions remain the source for
  business guarantees such as retention or job lifetime after disconnect.
- `OptionalDiscriminatorWarning` identifies optional discriminator fields in
  stream event unions, including nested and referenced events. Diagnostics name
  the operation, route, model, and field; generation preserves schema requiredness.
- Explicit `OperationErrorBody` aliases in both SDKs distinguish declared HTTP
  response bodies from thrown exceptions. Existing `OperationError` aliases remain
  available; TypeScript marks them deprecated. Method documentation lists the
  declared HTTP statuses and media types. Existing Python anonymous error-model
  imports such as `OperationError0` retain their names.
- TypeScript `isHttpError(error, isDetails)` narrows `HttpError.details` only after
  a caller-supplied type guard succeeds. It adds no runtime dependencies or assumed
  problem format. HTTP status and operation provenance remain caller checks.
- Checked TypeScript and Python error-handling consumer examples, plus a
  standalone FastAPI dependency/problem-response regression without an error
  framework. Public generated error-alias name collisions now fail explicitly.

### Changed

- Develop in `mathisarends/canon`; package source, documentation, and issue URLs
  now point to the monorepo.
- Single-value string fields now use TypeScript/Python literals instead of
  synthesizing a separate public constant object or `StrEnum` for every field.
  Explicitly named enums, multi-value field enums, and shared event discriminator
  enums remain available. TypeScript also omits unnecessary property quotes and
  union parentheses while preserving intersection grouping and extra-field types.

### Breaking generated-code changes

- Synthesized single-field constants such as `InvalidCredentialsProblemTitle`
  and `InvalidCredentialsProblemType` are no longer exported. Replace usages like
  `InvalidCredentialsProblemTitle.INVALID_CREDENTIALS` with the wire literal
  `"Invalid credentials"`, then remove unused imports. Python singleton fields
  now contain plain strings instead of generated `StrEnum` members; replace
  identity checks with value comparisons. Shared discriminator values such as
  `RunEventType.RUN_STARTED` and explicit schema enums are unchanged.
- Generated `OperationError`/`OperationErrorBody` names are reserved in both
  SDKs. Python now reports collisions explicitly too. If a schema uses one of
  these names, rename the colliding model or choose a distinct `operation_id`.

### Upgrade from 0.0.1

1. Update `fastapi-streams[codegen]` to `0.1.0`, using the same version for schema
   export and both SDKs.
2. Regenerate committed clients from the application's OpenAPI document. Existing
   method names and call signatures are unchanged:

   ```sh
   uv run fastapi-streams schema --app your_app:app --output openapi.json
   uv run fastapi-streams generate --schema openapi.json --language typescript --output generated/typescript
   uv run fastapi-streams generate --schema openapi.json --language python --output generated/python
   uv run fastapi-streams check --app your_app:app --schema openapi.json --language typescript --output generated/typescript
   uv run fastapi-streams check --app your_app:app --schema openapi.json --language python --output generated/python
   ```

   Substitute the application's existing module and output paths. Preserve
   `--python-http-client httpx2` on generation and checks if it is already used.
3. Review optional discriminator warnings. For output-only Pydantic event models
   whose defaults are always serialized, see the serialization configuration in
   [the codegen guide](docs/codegen.md#event-discriminators). Do not make optional
   request fields required merely because they have Python defaults. Add
   endpoint-specific guarantees to OpenAPI descriptions. Export and regenerate
   again after changing models or descriptions.
4. Apply the single-field constant migration above if the application imported
   these generated names. HTTP payloads, method names, and signatures are unchanged.
5. Prefer `OperationErrorBody` over `OperationError` when referring to response
   bodies. Keep caught errors unknown until checked; validate `HttpError.details`
   with a body guard in TypeScript or Pydantic `TypeAdapter` in Python. See
   [HTTP error contracts](docs/clients.md#http-error-contracts). Unexpected proxy
   responses must continue through the generic `HttpError` fallback.
6. Run the consuming project's TypeScript/Python type checks and stream tests.
   Commit the exported schema and regenerated SDKs together with call-site updates.

## 0.0.1

- Initial typed GET/POST JSON SSE routers, OpenAPI export, TypeScript and Python
  generators, shared transports, and contract/client drift checks.
