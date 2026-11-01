# Generate one domain SDK

Prepared Canon 0.10.0 introduces `fastapi-canon generate`. OpenAPI, OpenRPC and WebRTC
contracts contribute operations to a common plan, model graph and error runtime.
The generated public API follows domain names; protocol clients live under
`internal/`. See the [decision record](../CANONICALIZATION_DECISIONS.md) and
[server problem guide](../packages/fastapi-canon/docs/problems.md).

## Configuration

Use `fastapi-canon.toml` with a `[codegen]` section. In `pyproject.toml`, prefix
these tables with `tool.fastapi-canon`: `[tool.fastapi-canon.codegen]`,
`[tool.fastapi-canon.codegen.sources.api]`, and so on. The package name scopes
the configuration; `codegen` identifies the SDK generation settings.

```toml
[codegen]
version = 1

[codegen.sources.api]
kind = "openapi"
source = "app:app"
output = "contracts/openapi.json"

[codegen.sources.control]
kind = "openrpc"
source = "app:contract"
output = "contracts/openrpc.json"

[codegen.targets.browser]
language = "typescript"
sources = ["api", "control"]
output = "frontend/generated"

[codegen.targets.python]
language = "python"
sources = ["api", "control"]
output = "python/api_client"
package = "api_client"
```

Live sources use `module:attribute`; `factory = true` explicitly calls a factory.
OpenAPI/WebRTC exports accept an app. OpenRPC accepts the result of
`service.contract(..., app=app)`. Saved sources use `input = "contracts/api.json"`
instead of `source`/`output`. Kinds are `openapi`, `openrpc`, `webrtc`. Paths are
relative to the config file. Unknown settings fail explicitly.

Optional target settings are `client-name = "ApiClient"` and, for Python,
`http-client = "httpx"` or `"httpx2"`. `package` names the package at the Python
output path; put its parent on the import path. Generation needs Canon's
`codegen` extra. Generated SDKs do not import Canon: Python consumers need
Pydantic, the selected HTTP client, and `websockets` for WebSocket connections.
TypeScript needs fetch/DOM APIs and the ESNext.Disposable type library.

### Incremental adoption from an existing contract

Select operations by their original wire IDs before model planning and binding
generation. This lets a consumer adopt a supported part of an existing API
without editing the exported contract or importing/deploying the application:

```toml
[codegen.sources.api]
kind = "openapi"
input = "contracts/openapi.json"
exclude-operations = ["upload_attachment", "download_report"]

[codegen.sources.control]
kind = "openrpc"
input = "contracts/openrpc.json"
include-operations = ["projects.cancel", "projects.updates"]
```

`include-operations` is an optional non-empty allowlist; `exclude-operations`
removes explicitly named operations. OpenAPI uses `operationId`, OpenRPC uses
method/notification/subscription/socket names, and WebRTC uses session names.
Selection happens before public `operations` name mappings. Unknown IDs,
duplicate selectors, including and excluding the same ID, or an empty result
fail explicitly. There are no wildcard selectors or automatic exclusions.

Only selected operations and their referenced model closure reach the emitters;
recursive models remain intact. Existing mappings for excluded operations may
stay in the configuration. Saved inputs remain unchanged, and manifests retain
the original input hashes plus the selection settings. Selection changes are
covered by `--check`/`--diff`, ownership and stale-file cleanup.

Unsupported operations still fail generation unless explicitly excluded. For
example, leave file transfer on an existing adapter while adopting generated
JSON/SSE/RPC calls, then remove those exclusions when support is available.

## Commands and ownership

```console
fastapi-canon generate
fastapi-canon generate --config fastapi-canon.toml
fastapi-canon generate --config fastapi-canon.toml --check --diff
fastapi-canon generate --config fastapi-canon.toml --offline --check
fastapi-canon generate --config fastapi-canon.toml --target browser
```

Without `--config`, the command reads `fastapi-canon.toml` in the current
directory, then falls back to `pyproject.toml`. Python imports remain under
`fastapi_canon`; command and configuration names use the distribution's hyphens.

Live generation exports contracts before planning. Offline generation reads saved
outputs and never imports application code. `--target` is repeatable. `--diff`
implies read-only checking. Exit codes: 0 success, 1 drift, 2 configuration,
contract or dependency errors. Validate/render all selected targets before writes.

One `.canon-codegen.json` manifest owns each output. Preserve unowned files,
reject unowned collisions, and remove only stale owned files. Outputs cannot
overlap or traverse symlinks/junctions. Use a fresh output when replacing a
specialized SDK; old manifests are not adopted automatically. Generation does
not install dependencies or publish SDKs.

## Consumer API

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

```python
from api_client import ApiClient, ProblemError

async with ApiClient("https://api.example.com") as client:
    try:
        project = await client.projects.get(project_id=1)
        async for event in client.projects.events(project_id=project.id):
            render(event)
        await client.projects.cancel(project_id=project.id)
    except ProblemError as error:
        show_problem(error.problem)
```

Run the [complete example configuration](../packages/fastapi-canon/examples/canonical_errors/fastapi-canon.toml)
with `fastapi-canon generate --config packages/fastapi-canon/examples/canonical_errors/fastapi-canon.toml`.
Its generated output goes into the example's ignored `dist/` directory.

TypeScript output contains `index.ts`, `client.ts`, `models.ts`, `errors.ts`, and
private binding modules under `internal/`. Python has corresponding package
modules. Canonical model declarations and the base error runtime are shared
within this output. Separate SDK outputs still have separate class identities.

Calls return Promises/awaitables, streams expose async iteration, and sessions
return explicit socket/media objects. TypeScript session signatures retain
binding connect options and return types. Python socket/binary sessions use
async context managers. No `sse`/`rpc` prefix is added to domain methods.

## Names and shared models

OpenAPI naming uses configuration, `x-canon-operation`, dotted operation IDs, or
first tag plus operation ID. JSON-RPC and WebRTC use declared operation/session
names. Override explicitly when adapting existing contracts:

```toml
[codegen.sources.api.operations]
get_project = "projects.get"
watch_project = "projects.events"

[codegen.sources.control.operations]
"maintenance.cancelProject" = "projects.cancel"
```

Conflicting normalized operation/group names fail. Similar-looking models do not
merge automatically. Assign semantic identity with `x-canon-model-id`, `$id`, or:

```toml
[codegen.sources.api.identities]
Project = "urn:example:project-output:v1"

[codegen.sources.control.identities]
ProjectView = "urn:example:project-output:v1"
```

A shared Pydantic DTO should declare its wire aliases and extra-field policy
explicitly so HTTP and RPC export the same shape, for example:

```python
class Project(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        alias_generator=to_camel,
        populate_by_name=True,
        json_schema_extra={"x-canon-model-id": "urn:example:project-output:v1"},
    )
    project_id: int
```

Import `ConfigDict`/`BaseModel` from Pydantic and `to_camel` from
`pydantic.alias_generators`. A semantic ID does not erase differences between
wire schemas; use separate identities for intentionally different views.

Identical identities require compatible schemas. Different identities with the
same public name require `names = { Project = "ArchivedProject" }` on the relevant
source. Use separate identities for incompatible input/output shapes. Problems
share their type URI automatically, including nested extensions. HTTP status
variants normalize to optional client status; embedded wire problems still omit it.

## Connections and supported scope

Bindings open lazily. `baseUrl` drives HTTP/SSE and an unambiguous single JSON-RPC
endpoint. Override WebSocket connection settings separately:

```typescript
const client = createClient({
  baseUrl: "https://api.example.com",
  bindings: { control: { connect: { url: "wss://control.example.com/ws" } } },
});
```

Multiple servers, template variables and authentication use typed binding
`connect` options. HTTP headers are not forwarded to WebSockets. An injected
`bindings.control.client` stays caller-owned unless `owned: true` is set. Python
accepts `http_client=`, `clients={"control": existing_client}` and
`bindings={"control": {"url": ...}}`; injected Python clients stay caller-owned.
Closing the facade prevents new work and closes the resources it created.

HTTP/SSE request options and JSON-RPC call options retain their binding's
semantics. Python uses task cancellation and optional timeouts; TypeScript calls
and SSE support `AbortSignal`. Accepted raw/WebRTC sessions retain their protocol
close/error semantics and do not receive SSE terminal frames.

Unified HTTP generation supports JSON bodies/responses, scalar path/header
parameters and exploded scalar-array query parameters. Multipart/files, cookies,
other parameter styles and unsupported schema constructs fail preflight.
Specialized generators remain available for their additional binding options
and use separate output ownership. WebRTC targets TypeScript; requesting Python
fails before writing.

Common DTOs use JSON wire scalar types: UUID/date-time values are strings in
both language targets, including RPC primitive results. Python names use snake
case and TypeScript operation arguments use camel case; binding wire names stay
explicit. Python uses Pydantic validation. TypeScript models are static types, with runtime
checks for standard problem members and SSE controls, not a full JSON Schema
validator. Replay, automatic manifest migration and structured JSON diagnostics
remain future work.

Python string constraints use Pydantic's default regex engine where supported,
with compiled Python patterns for lookarounds and backreferences. This includes
decimal-string schemas exported by earlier Pydantic versions. Constraints stay
active in model fields, nested arrays and standalone type aliases; invalid
patterns fail generation. Decimal wire values stay strings, preserving precision.

HTTP clients omit optional query/header parameters whose value is null. Required
query/header parameters need a non-null value; a nullable JSON body still sends
JSON `null`.
Python validates each response against the schema for its actual status. An
exact status declaration takes precedence over `2XX` in both language targets.

TypeScript HTTP methods distinguish bodyless responses (`void`/`undefined`) from
JSON `null` (`null`). Operations supporting both JSON and bodyless success
statuses return the JSON type or `void`. Empty or malformed JSON and unexpected
bodies for bodyless contracts raise `ProtocolError`.
Python also rejects empty JSON and unexpected bodies for bodyless contracts.
HTTP connection failures, including interrupted response bodies and problem
responses, raise `TransportError`. TypeScript retains native abort errors for
explicit cancellation and request timeouts.
