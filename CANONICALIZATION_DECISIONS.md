# Canon canonicalization decisions

Decision date: 2026-10-31.

Status: approved design and implementation record for PR #6, prepared Canon
0.10.0. The common generation workflow below is implemented in this PR. Verify
the checked-out source and current-head tests when continuing work.

## 1. Purpose and authority

The maintainer wants the four merged codebases to feel like one coherent
FastAPI library. The work concerns composition, exception mapping, HTTP and
stream error contracts, code generation, and the generated consumer experience.
The clarified request is a design/coherence review, not an investigation of a
reported production regression. Breaking changes are explicitly allowed.

Read this document for the decisions made in the discussion. Read the checked-out
source, tests, `AGENTS.md`, and implemented package guides for what actually exists.
In the consolidated repository, `docs/canon.md` records current package boundaries
and `docs/unified-canon-concept.md` documents the implemented common generation workflow.
The final client-facing direction below supersedes the transport-prefixed and
feature-client alternatives discussed earlier in the chat.

The maintainer paused implementation to discuss and record this design, then
explicitly authorized the first error-canonicalization PR. This first step is
expanded to shared RFC 9457 errors, RPC and terminal SSE bindings, and their
generated-client handling; see the implementation update below. Combined SDK
orchestration is included by the maintainer's subsequent approval. Publication
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

`RpcChannel(error_registry=errors)` consumes this catalog. Channel/method
`raises=` accepts the same `Error` definitions. Composition collects mounted
channel registries; export with `service.contract(..., app=app)` to use its
resolved type URIs. Catalog rendering helpers are internal adapter APIs, not
application APIs and must not be the focus of PR descriptions.

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
The exact occurrence-ID policy remains open. Canon's profile requires type,
title, and code, and includes status only for an actual HTTP error response.

## 5. Shared semantics with explicit protocol bindings

HTTP, JSON-RPC, SSE, raw/binary sockets, and WebRTC have different framing and
lifecycle rules. Reuse problem identity and mapping through documented bindings.

- HTTP: return an RFC 9457 HTTP problem response before the response starts.
- JSON-RPC: preserve the JSON-RPC envelope, integer `error.code`, and
  `error.message`. The implemented binding places the shared problem under
  `error.data.problem`. Canonical application failures use server code -32000;
  the problem type URI identifies the application failure. Existing protocol
  errors retain their numeric JSON-RPC codes.
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

Declaration shape implemented by the first canonical error PR:

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
protocol conventions, not event names defined by RFC 9457.

Generated clients consume these control frames, yield ordinary application
events, raise `ProblemError` for a terminal problem, and stop on completion.
They must not silently discard terminal controls as unknown business events.
A terminal problem does not trigger automatic retry/reconnect. A native browser
`EventSource` needs an explicit `close()` to avoid unwanted reconnection;
the generated adapter should handle that.

Profile version 1 uses canon.complete with an empty JSON object and reserves
canon.* event names. OpenAPI describes all three frame variants and advertises
the business and terminal problem schemas through x-canon-stream. Raw
StreamRouter endpoints keep their unprofiled behavior. Regeneration is required
for SDKs to understand Canon controls. Replay and resumption remain future work.
See the [SSE processing model](https://html.spec.whatwg.org/multipage/server-sent-events.html#processing-model).

## 7. Domain operations, interaction, and binding

Keep three concepts separate:

| Concept | Examples |
| --- | --- |
| Domain operation | `projects.get`, `projects.cancel`, `projects.events`, `rooms.join` |
| Interaction | `call`, `stream`, `session` |
| Binding | HTTP JSON, SSE over HTTP, JSON-RPC over WebSocket, raw WebSocket, WebRTC |

RPC is an interaction model; JSON-RPC is a concrete protocol; WebSocket is a
transport. Streams are not limited to HTTP. These are not parallel public domain
namespaces. The accepted generated API is:

```typescript
import { createClient, ProblemError } from "./generated";
const client = createClient({ baseUrl: "https://api.example.com" });
try {
  const project = await client.projects.get({ projectId: 1 });
  for await (const event of client.projects.events({ projectId: project.id })) {
    render(event);
  }
  await client.projects.cancel({ projectId: project.id });
} catch (error) {
  if (error instanceof ProblemError) showProblem(error.problem);
  else throw error;
} finally {
  await client.close();
}
```

This is one facade with internal binding clients, one shared problem/error
runtime and one canonical model graph per output. It does not multiplex different
protocols onto one connection. Independent language/output targets have separate
class identities. Specialized generators remain available; two independently
generated SDKs do not share their error class identities.

Calls return Promises/awaitables; streams expose async iteration; sessions retain
explicit socket/media objects and their own close/channel APIs. Python sessions
use async context managers. Server feature names, directory structure, transport
prefixes and URL paths do not prescribe the client's domain namespaces.

## 8. Common generation and model identity

`canon generate --config canon.toml` is the canonical workflow. Live sources export
reviewable protocol contracts; saved inputs and `--offline` avoid application
imports. A common plan resolves operations, schemas and errors before language
and binding emitters render one SDK. The exact configuration and supported scope
are in [the codegen guide](docs/unified-canon-concept.md).

OpenAPI names use explicit configuration, `x-canon-operation`, dotted operation
IDs, or first tag plus operation ID, in that order. JSON-RPC uses declared method
names; WebRTC uses session names. Explicit source `operations` mappings override
those defaults. Reject normalized name and method/group collisions. Do not
silently select an alternative binding or infer fallback behavior.

Merge models through explicit semantic identity (`x-canon-model-id`, `$id`, or
source `identities` settings), not structural similarity. Different identities
with colliding public names require explicit `names` mappings. Identical
identities require compatible schemas. Keep input/output identities separate
when their wire schemas differ. Problem type URIs supply shared identity;
nested extension definitions retain identity across exporter-specific layouts.
Canonical model declarations are generated once and reused by binding modules.
Use one acronym/name policy and wire aliases when building RPC parameter models;
Python common DTOs use snake case, with BaseModel name collisions escaped.
UUID/date-time scalars use JSON wire strings consistently across bindings.
Binding-specific internal aliases may remain local.

## 9. Lifecycle and errors

Bindings open lazily. HTTP/SSE and an unambiguous single JSON-RPC endpoint share
the configured base URL; explicit binding connection settings override it.
Credentials, template variables and multiple servers remain binding options.
HTTP headers are not automatically forwarded to WebSockets.

The facade closes resources it creates, rejects work after close, and tracks
active work and sessions. Injected clients stay caller-owned; TypeScript can
explicitly mark an injected binding owned. Preserve per-binding request/session
options and cancellation semantics. Do not pretend accepted raw/WebRTC sessions
have SSE or JSON-RPC framing.

Every binding uses the same generated `ProblemError` base and public problem
shape within one output. Protocol-specific diagnostics may subclass the common
transport errors. Unknown problem types and extensions remain available; a known
`code` alone cannot select a known subtype with a different URI.

Python validates received data with Pydantic. TypeScript provides static model
types and runtime checks for standard problem members and SSE controls; it does
not claim complete JSON Schema runtime validation.

## 10. Determinism, ownership, and supported scope

Version-1 `canon.toml` configuration (or `[tool.canon]` in `pyproject.toml`) names
`sources` and language `targets`. Paths resolve relative to the configuration.
Unknown settings, incompatible schemas, ambiguous names and unsupported targets
fail explicitly. Validate/render all selected targets before writing. Exit codes
are 0 success, 1 drift, 2 invalid configuration/contracts/dependencies.
`--check` is read-only; `--diff` implies check.

One `.canon-codegen.json` manifest owns each SDK output and records input hashes,
generator settings and files. Preserve handwritten files, reject unowned
collisions, remove only stale owned files, and reject overlapping outputs and
symlink/junction traversal. Use a fresh output or explicit migration for old
specialized SDKs; ownership of predecessor manifests is not assumed. Never
install dependencies or publish SDKs automatically.

The unified pipeline supports JSON HTTP calls, typed SSE, JSON-RPC calls and
subscriptions, raw/binary sessions, and TypeScript WebRTC. Python WebRTC fails
before writing. Unsupported media, parameter styles and schema constructs fail
preflight. Specialized emitters remain available for their additional options.
Multipart/file clients, automatic manifest migration, complete JSON Schema
runtime validation in TypeScript, machine-readable diagnostic output and SSE
replay remain future work.

## 11. Implementation and review ownership

This work belongs to [PR #6](https://github.com/mathisarends/fastapi_canon/pull/6),
on `codex/canonical-error-contracts` in `dist/pr-canonical-errors`, based on fetched
`origin/main`. Prepared version: 0.10.0. No merge, release or publication is
authorized by this task. Preserve the older draft worktrees; do not merge their
partial ports accidentally. Verify current main ancestry and current-head CI.

PR descriptions must show actual developer APIs: define errors, register HTTP,
SSE and RPC operations, compose the app, generate one SDK, and consume domain
methods. Do not replace this with catalog-internal rendering examples. Test
actual generated Python and TypeScript calls, common model/error identity,
stream controls and cleanup, offline generation, drift and file ownership.
Do not reuse old-checkout validation as proof of the current PR.
