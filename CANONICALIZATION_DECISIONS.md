# Canon canonicalization decisions

Decision date: 2026-10-31.

Status: agreed architectural direction from the maintainer discussion, with
explicitly marked API sketches and unresolved details. This document is an agent
handoff and design record. It does not claim that the target APIs are implemented.

## 1. Purpose and authority

The maintainer wants the four merged codebases to feel like one coherent
FastAPI library. The work concerns composition, exception mapping, HTTP and
stream error contracts, code generation, and the generated consumer experience.
The clarified request is a design/coherence review, not an investigation of a
reported production regression. Breaking changes are explicitly allowed.

Read this document for the decisions made in the discussion. Read the checked-out
source, tests, `AGENTS.md`, and implemented package guides for what actually exists.
In the consolidated repository, `docs/canon.md` records current package boundaries
and `docs/unified-canon-concept.md` contains the earlier codegen discussion draft.
The final client-facing direction below supersedes the transport-prefixed and
feature-client alternatives discussed earlier in the chat.

The maintainer paused implementation to discuss and record this design, then
explicitly authorized the first error-canonicalization PR. This first step is
scoped to the shared RFC 9457 problem foundation; see the implementation update
below. SDK orchestration and new protocol bindings remain later work. Publication
is not authorized.

## 2. One product and one composition model

- Maintain one `fastapi-canon` distribution and one Canon version.
- Keep optional integrations under `fastapi_canon.integrations`: SSE,
  WebSockets, WebRTC, and telemetry.
- Extras select third-party dependencies. Core imports and root CLI help must
  remain usable without optional generators, telemetry SDKs, or media engines.
- Keep application domain code framework-free. Domain code raises ordinary,
  specific application exceptions.
- Features contribute routers, error definitions, Dishka providers, handlers,
  and lifespans. Composition is the application ownership boundary.
- Provide a clear canonical path for application authors and agents. Lower-level
  transport APIs may remain available for deliberate integration requirements.
- A shared abstraction must have concrete reuse; consolidation alone is not a
  reason to force unrelated protocol behavior into the same implementation.

## 3. One catalog of public domain problems

The selected direction is a shared mapping from application exceptions to public
problem definitions, reused across HTTP, SSE, and RPC. Applications should not
need separate business exception hierarchies solely for each transport.

The existing declaration style is the starting point:

```python
class ProjectMissing(Exception):
    pass


project_missing = Error(
    ProjectMissing,
    type="https://api.example.com/problems/project-missing",
    code="project_missing",
    title="Project not found",
    status=404,
)

project_errors = ErrorRegistry(errors=[project_missing])
```

The exception is a domain concern. The mapping is an API-contract concern.
The HTTP status in this example is HTTP mapping metadata; it does not become an
HTTP response status for an RPC invocation or an already-running stream.

Router/operation declarations identify the applicable public failures. The same
definitions should drive runtime rendering, exported schemas, and generated
problem types. Keep registry membership and collision checks explicit.
`raises=` is a contract declaration, not an exhaustive checked-exception system.

The exact extension of the existing RPC declaration API to consume this catalog
is still to be designed. Do not present a hypothetical `RpcService` constructor
or decorator as an implemented API.

## 4. RFC 9457 is the HTTP foundation

Use [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457.html) as the basis for HTTP
Problem Details and as the semantic model reused by other transport bindings.

| Member | Canon design rule |
| --- | --- |
| `type` | Primary problem-type identity. Use a stable URI, preferably absolute. |
| `title` | Human-readable summary of the type, stable except for localization. |
| `status` | HTTP status of this occurrence, when present. Match the actual HTTP response. |
| `detail` | Reviewed, occurrence-specific public explanation. Clients must not parse it for control flow. |
| `instance` | Optional URI reference identifying a particular occurrence. |
| `code` | Stable Canon extension for convenient application handling; it does not replace `type`. |
| Other extensions | Explicitly described structured public data; consumers tolerate unknown extensions. |

HTTP problem responses use `application/problem+json`. A Canon emission profile
may require more information than the RFC minimum, but its extra requirements
must be documented as Canon policy rather than attributed to the RFC.

For example:

```http
HTTP/1.1 404 Not Found
Content-Type: application/problem+json

{
  "type": "https://api.example.com/problems/project-missing",
  "title": "Project not found",
  "status": 404,
  "code": "project_missing",
  "detail": "The requested project does not exist.",
  "instance": "urn:uuid:983cf916-6e8e-4d8f-a836-e9708d160890"
}
```

Exception messages, rejected input, stack traces, credentials, and internal
implementation details must not be exposed automatically. Unexpected server
failures get a safe public problem while retaining useful internal diagnostics.
The exact occurrence-ID policy and the required members of Canon's profile
remain open.

## 5. Shared semantics with explicit protocol bindings

HTTP, JSON-RPC, SSE, raw/binary sockets, and WebRTC have different framing and
lifecycle rules. Reuse problem identity and mapping through documented bindings.

- HTTP: return an RFC 9457 HTTP problem response before the response starts.
- JSON-RPC: preserve the JSON-RPC envelope, integer `error.code`, and
  `error.message`. The discussed binding places the shared problem under
  `error.data.problem`; the exact wrapper and numeric-code policy are provisional.
- SSE: use the terminal problem-event profile described below after stream start.
- WebSocket/WebRTC admission: use applicable pre-accept HTTP problem handling
  where the server and protocol support it. Accepted sessions retain their
  protocol-specific lifecycle and failure semantics.

References: [JSON-RPC error objects](https://www.jsonrpc.org/specification#error_object)
and [embedding Problem Details in other formats](https://www.rfc-editor.org/rfc/rfc9457.html#appendix-C).

Do not silently insert SSE control events into RPC binary streams, raw sockets,
media tracks, or existing WebRTC signaling messages. The stream decisions below
specifically concern Canon's SSE profile. Changes to other wire protocols need
an explicit binding and migration.

## 6. Canon SSE error and completion profile

The agreed direction is to canonicalize failures both before and after stream
start using the same catalog.

| Situation | Target behavior |
| --- | --- |
| Authentication, authorization, or lookup fails before response start | HTTP Problem Details response. |
| A declared processing exception occurs during streaming | Send one terminal `canon.problem` event, then close and clean up. |
| Generator finishes normally | Send `canon.complete`, then close and clean up. |
| Unexpected processing exception occurs | Log internally; send a safe terminal internal problem if transmission is still possible. |
| Unexpected connection termination | Client reports a transport failure; successful completion must not be inferred from EOF alone. |
| Consumer cancels or disconnects | Propagate cancellation and clean up; do not manufacture a business problem. |

Proposed declaration shape, accepted as the direction but not yet implemented:

```python
@router.stream(
    "/runs/{run_id}/events",
    raises=[run_not_found],
    stream_raises=[run_failed],
    dependencies=[Depends(require_existing_run)],
)
async def events(run_id: str) -> AsyncIterator[RunEvent]:
    async for event in service.events(run_id):
        yield event
```

- `raises=` describes HTTP failures before response start.
- `stream_raises=` describes terminal processing failures carried inside SSE.
- Both refer to definitions from the same error catalog.
- Perform preconditions in dependencies: generator code, including statements
  before the first `yield`, runs after the streaming response has started.

Example terminal frame:

```text
event: canon.problem
data: {"type":"https://api.example.com/problems/run-failed","title":"Run failed","code":"run_failed"}

```

The embedded problem omits `status`. The HTTP response status is already fixed;
the catalog's HTTP error status must not be represented as a new status for that
response. SSE keeps its `text/event-stream` media type. Embedding uses the RFC
model following Appendix C; `canon.problem` and `canon.complete` are Canon's
proposed protocol conventions, not event names defined by RFC 9457.

Generated clients consume these control frames, yield ordinary application
events, raise `ProblemError` for a terminal problem, and stop on completion.
They must not silently discard terminal controls as unknown business events.
A terminal problem does not trigger automatic retry/reconnect. A native browser
`EventSource` needs an explicit `close()` to avoid unwanted reconnection;
the generated adapter should handle that.

The exact completion payload, profile version/metadata, reserved-name rules,
resumption/replay behavior, and compatibility with native/unprofiled SSE remain
to be designed. Existing SDKs cannot be assumed to understand these new frames.
See the [SSE processing model](https://html.spec.whatwg.org/multipage/server-sent-events.html#processing-model).

## 7. Final generated client direction: a domain-facing API

The final recommendation after discussion is one generated SDK per selected
language/target, with a common domain-facing client and operation groups.
Transport adapters remain separate internally.

Illustrative TypeScript target API, not an implemented signature:

```typescript
const client = new Client({
  baseUrl: "https://api.example.com",
});

try {
  const evaluation = await client.evaluations.start({ input });

  for await (const event of client.evaluations.events({
    evaluationId: evaluation.id,
  })) {
    render(event);
  }
} finally {
  await client.close();
}
```

The call site selects the capability. The generated contract selects its
transport binding. The observable result is explicit in the type:

```typescript
start(input): Promise<Evaluation>
events(input): AsyncIterable<EvaluationEvent>
```

These are signature sketches. Exact names, overloads, constructor options, and
connection factories are not frozen by the examples.

Public API groups come from the public contract and explicit generation
configuration. They do not automatically mirror server-side Canon `Feature`
objects, Python modules, URL prefixes, or connection boundaries.

The existing WebSocket generator already produces one client for a contract with
namespaces, such as `AutomationClient.tasks` and `.browser`. It does not imply
one independently connected client per feature. Preserve that useful distinction.

## 8. Earlier client alternatives are superseded

Do not treat the following earlier examples as the selected default:

```typescript
client.sse.evaluations.events(...)
client.websockets.rpc.evaluations.start(...)
rpc.evaluations.start(...)
streams.evaluations.events(...)
```

Separate `RunsRpcClient` and `RunsStreamClient` classes per business feature were
also explored and then superseded as the default direction. A monolithic public
transport hierarchy and mandatory feature-specific client instances are not the
goal.

The selected public shape is:

```typescript
client.evaluations.start(...)
client.evaluations.events(...)
```

An application can pass `client.evaluations` to code that only needs that API
group. Low-level transport access can remain available for explicit use cases.
RPC is a protocol and WebSocket is a transport; neither distinction needs to
appear as an extra level in ordinary domain method calls.

## 9. Lifecycle, cancellation, and transport behavior remain explicit

Removing transport prefixes does not erase their observable behavior.

- Constructing the common client should not eagerly connect every integration.
- Open connections on demand or through clearly documented connection methods.
- Close resources owned by the SDK. Injected clients/transports remain owned by
  their caller unless ownership transfer is explicit.
- Expose clear cancellation for calls, streams, and sessions.
- Document connection state, reconnect, deadlines, retries, and resumption.
- Keep transport-specific configuration and diagnostics accessible.
- Retain meaningful connection/session objects when the application explicitly
  manages sockets or media sessions.

One shared client does not imply one physical connection. Connection pooling and
reuse remain internal transport responsibilities. A successful reconnection does
not by itself promise gap-free event replay. Retries must account for operation
semantics and idempotency; do not apply a blanket retry policy to all methods.

Authentication must respect platform capabilities. For example, browser
WebSockets cannot attach arbitrary HTTP request headers. A shared facade must
not promise identical credential mechanisms for every transport.

## 10. Common generated errors and shared models

Use a shared generated problem model and shared error classes within an SDK.
The working error vocabulary is:

| Error | Meaning |
| --- | --- |
| `ProblemError` | A remote application problem carried by a documented transport binding; exposes `.problem`. |
| `TransportError` | Connection failure, timeout, or unexpected interruption. |
| `ProtocolError` | Received data violates the agreed protocol or declared contract. |

Cancellation remains recognizable as cancellation. Exact class names and
language-specific inheritance details need final API review.

HTTP, RPC, and SSE adapters extract the same semantic problem representation.
Use `type` as the primary problem-type identity; expose `code` as a convenience
extension. Preserve transport diagnostics separately rather than inventing HTTP
statuses for non-HTTP failures. Runtime validation must support the guarantees
promised by generated types and helpers.

Generate shared models/problems once per SDK target and import them from its
transport implementations. In particular, avoid duplicate `ProblemError` class
definitions that make `instanceof` depend on which transport created an error.

Share domain schemas only when semantic identity and serialization are
compatible. Matching names or shapes alone are insufficient. Input and output
schemas may differ in aliases, defaults, and required fields. Report conflicts
with source locations. Unknown problem types/extensions need a documented
forward-compatibility policy; do not use unchecked casts to claim a known type.

## 11. One code generation workflow

The selected direction is one workflow around the existing emitters:

```text
Configured sources or saved contracts
  -> export/read and validate contracts
  -> validate a complete generation plan
  -> resolve names, shared models, and problems
  -> render the selected language/transport implementations
  -> write SDK files through one ownership boundary per output
```

Keep OpenAPI (including native SSE schemas), OpenRPC, and WebRTC contracts as
reviewable artifacts. A common workflow does not require a new universal wire
protocol or the loss of protocol-specific schema information.

Discussed commands, not current implemented command claims:

```console
canon generate
canon generate --target frontend
canon generate --check --diff
```

The full workflow should use current live exports when sources are configured,
so checking against an old saved contract cannot conceal application drift.
Also support generation from saved contracts without importing application code
or requiring unrelated optional engines. Separate export/offline stages are
useful; names such as `canon contracts export` and `canon clients generate`
remain provisional.

## 12. One versioned configuration, named targets

A shared `[tool.canon]` configuration in `pyproject.toml` is the preferred
direction. It names contracts, language targets, outputs, and scoped options.
The exact spelling below is a discussion sketch:

```toml
[tool.canon]
config-version = 1

[tool.canon.contracts.http]
kind = "openapi"
source = "app.main:app"
output = "contracts/openapi.json"

[tool.canon.contracts.rpc]
kind = "openrpc"
source = "app.contracts:rpc"
output = "contracts/rpc.json"

[tool.canon.clients.frontend]
language = "typescript"
contracts = ["http", "rpc"]
transports = ["sse", "websockets"]
output = "frontend/src/generated"
```

The `transports` list selects internal generation capabilities, not public
property names such as `client.sse`. Additional targets may select Python or a
different contract subset. A saved input alternative should support offline
generation. Resolve paths relative to the configuration file.

Reject unknown keys, unsupported targets, ambiguous public names, incompatible
schemas, unsafe paths, and overlapping ownership before writing. If multiple
transports expose a candidate for the same public method, require an explicit
selection; do not choose or fall back based on naming guesses.

## 13. Determinism, diagnostics, and file ownership

- The same inputs and generator/configuration versions produce the same output.
- Validate and render all selected targets before starting output writes.
- `--check` is read-only, including files, directories, manifests, and reports.
- `--diff` shows drift in check mode.
- Use consistent exit codes: `0` success, `1` drift, `2` configuration,
  dependency, or contract errors. Errors take precedence over drift.
- Provide readable diagnostics and a machine-readable JSON form containing a
  stable diagnostic code, source location, and actionable cause.
- Keep one owner/writer per generated SDK output. Preserve handwritten files;
  remove only stale files recorded as generator-owned.
- Record contract hashes, generator/configuration versions, options, generated
  files, and consumer runtime requirements in deterministic ownership metadata.
- Migrate existing ownership manifests explicitly. The new manifest filename
  and exact output-directory layout remain open.
- Do not automatically install missing dependencies or publish generated SDKs.

## 14. Actual codegen support and implementation sequence

The consolidated source inspected during the discussion has the `canon sse`,
`canon websockets`, and `canon webrtc` command families. SSE and WebSocket
generation support Python and TypeScript; the existing WebRTC emitter targets
TypeScript. Ordinary REST client generation currently uses separate tooling.

The proposed domain-client examples describe the target experience. They do not
prove a complete Canon REST emitter or a combined SDK already exists. Adding
ordinary REST generation is a distinct implementation decision. Unsupported
combinations, including WebRTC for Python, must fail explicitly.

The recommended sequence discussed was:

1. Agree the public contracts, problem mappings, and SSE profile.
2. Implement shared configuration, planning, validation, diagnostics, and ownership
   around the existing generators.
3. Implement the domain-facing SDK facade and shared models/problem runtime.
4. Update language/transport emitters and verify real consumer behavior.

This is sequencing guidance, not permission to mark every stage complete or to
expand a narrowly scoped change into every possible emitter.

## 15. Acceptance expectations for future implementation

- Inspect the current checkout and applicable instructions before changing APIs.
- Verify runtime problem payloads against exported schemas, including shared
  statuses and typed extensions.
- Verify the same domain problem identity through each implemented binding.
- Test SSE admission failures, terminal problems, normal completion, unexpected
  EOF, malformed frames, cancellation, and cleanup.
- Generate, compile/typecheck, and execute real Python/TypeScript consumer paths.
  Successful generation alone does not establish runtime interoperability.
- Check shared class identity, naming collisions, input/output model differences,
  and forward compatibility.
- Check deterministic output, drift detection, offline behavior, and preservation
  of handwritten files.
- Preserve optional-dependency boundaries and verify actual built artifacts in
  fresh environments.
- Follow the current repository's versioning and migration policy for breaking
  APIs, defaults, generated clients, and wire-profile changes. Permission for
  breaking changes does not waive migration documentation or release checks.

## 16. Open details and handoff state

Open details include the precise SDK constructor/connection API; public-group
metadata and name mapping; shared model identity across contract formats;
RPC numeric-code/wrapper rules; Canon profile required fields and occurrence
identifiers; SSE profile versioning and replay semantics; config and manifest
syntax; output layout; runtime validation strategy; and the scope of a future
ordinary REST emitter.

Additional core changes appeared in an early implementation draft: normalization
without a domain registry, a default problem URI base, automatic startup OpenAPI
validation, a common configuration-error base, nested registry collection,
`http_statuses=`, and lifecycle/install checks. These are not all individually
ratified by this design discussion. Review them against the final model rather
than treating the draft patch as the agreed specification.

At the time of this record:

- The original checkout was an old 0.4.0 snapshot. Its draft changes were saved
  locally on `codex/coherent-contracts-source` in commit `33564bf`.
- Current consolidated source was fetched and an isolated worktree created at
  `dist/pr-coherent-canon`, on `codex/coherent-canon-contracts`, based on
  `af2d5d3` from `origin/main` (prepared, unpublished Canon 0.9.0).
- Transferring the old patch to the new package layout was interrupted for the
  design discussion. The worktree contains a partial port, including unresolved
  Git index entries. It is not a finished or reviewed implementation.
- The earlier report of 249 passing tests applies to the old checkout, not to
  the completed merged-stack target. Do not reuse it as validation of that target.
- No PR has been created for this canonicalization work and no release has been
  published. Future agents must reassess the actual state before continuing.

Re-check these historical handoff facts when resuming; subsequent repository
changes may supersede the recorded branch, commit, and worktree state.

## 17. First implementation step after design approval

The maintainer authorized starting with error canonicalization and creating an
isolated PR, with current `origin/main` included. A fresh worktree at
`dist/pr-canonical-errors`, branch `codex/canonical-error-contracts`, was created
directly from fetched main `af2d5d3`. The interrupted broader port described in
section 16 is preserved separately and is not the basis of this PR.

The first step prepares Canon 0.10.0 according to the repository's minor-version
policy. It supplies `ErrorRegistry.from_app()`, `problem_for()`, and `schema_for()`;
uses resolved `type` URIs for compiled HTTP unions; supports embedded problems
without an HTTP status; and preserves null extension values. HTTP compilation
and WebRTC admission reuse the shared schema compiler. The implemented guide is
`packages/fastapi-canon/docs/problems.md`.

This foundation does not implement the RPC problem envelope, SSE terminal control
frames, or combined generated client facade. Their design status above remains
explicit. PR creation, test results, and remote main ancestry must be checked
from the actual GitHub/Git state instead of inferred from this record.
