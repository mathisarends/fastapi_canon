# Canonical errors

Define a domain exception once, map its public problem once, and declare that
same `Error` on HTTP, SSE, and RPC handlers. Canon 0.10.0 prepares these bindings
and their Python/TypeScript generated-client support; it is not a publication.
The complete executable example is in
[`examples/canonical_errors/app.py`](../examples/canonical_errors/app.py).

## Define the public error

```python
from fastapi_canon import Error, ErrorRegistry


class ProjectMissing(Exception):
    pass


project_missing = Error(
    ProjectMissing,
    status=404,
    code="project_missing",
    title="Project not found",
    detail="Choose another project.",
    extensions={"replacement": None},
)
errors = ErrorRegistry(
    errors=[project_missing],
    type_base="https://api.example.com/problems",
)
```

The exception stays framework-free. Its exception text is private by default;
only the declared detail and extension renderers produce public data. Use
`extensions_model=` for typed dynamic extension fields. Explicit null extension
values are preserved.

[RFC 9457](https://www.rfc-editor.org/rfc/rfc9457.html) makes `type` the primary
problem identity. Canon's emission profile requires `type`, `title`, and its
`code` extension. These additional profile requirements do not come from the RFC.
Titles describe the type; public details describe an occurrence. Consumers must
not parse either string for control flow.

## Declare errors where an operation can fail

```python
from collections.abc import AsyncIterator

from fastapi import Depends
from pydantic import BaseModel

from fastapi_canon import CanonRouter
from fastapi_canon.integrations.sse import CanonStreamRouter
from fastapi_canon.integrations.websockets import RpcChannel

http = CanonRouter(error_registry=errors)
streams = CanonStreamRouter(error_registry=errors)
projects = RpcChannel("projects", error_registry=errors)


class ProjectEvent(BaseModel):
    sequence: int


class CancelProject(BaseModel):
    project_id: int


def require_project(project_id: int) -> None:
    if project_id == 0:
        raise ProjectMissing("private database information")


@http.get("/projects/{project_id}", raises=[project_missing])
async def get_project(project_id: int) -> dict[str, int]:
    require_project(project_id)
    return {"id": project_id}


@streams.stream(
    "/projects/{project_id}/events",
    operation_id="events",
    tags=["projects"],
    raises=[project_missing],
    dependencies=[Depends(require_project)],
    stream_raises=[project_missing],
)
async def events(project_id: int) -> AsyncIterator[ProjectEvent]:
    yield ProjectEvent(sequence=1)
    if project_id == 2:
        raise ProjectMissing("private database information")


@projects.method(raises=[project_missing])
async def cancel(params: CancelProject) -> None:
    require_project(params.project_id)
```

For SSE, `raises=` documents HTTP admission failures from dependencies;
`stream_raises=` declares terminal processing failures. A generator's body runs
after streaming starts, including the code before its first yield. Put admission
checks in dependencies. Sync and async generators use the same terminal profile.

RPC's existing `RpcError` declarations remain available for protocol-specific
contracts. Application domain errors use the shared `Error` definitions above;
there is no second domain-to-RPC mapping to maintain.

## Compose the application and export its contracts

```python
from fastapi import APIRouter, FastAPI

from fastapi_canon import Composition, Feature
from fastapi_canon.integrations.websockets import RpcService
from fastapi_canon.integrations.websockets.adapters.fastapi import RpcRoutes

service = RpcService()
endpoint = service.socket("/ws", channels=[projects])
websockets = APIRouter()
RpcRoutes(websockets).mount(endpoint)

app = Composition(
    Feature(name="projects", routers=[http, streams, websockets]),
).apply(FastAPI())

contract = service.contract(
    title="Projects",
    base_url="wss://api.example.com",
    app=app,
)
```

HTTP and stream routers contribute their catalogs. Mounted RPC channels also
contribute theirs to the feature. Composition checks conflicts and installs the
resolved application catalog. A feature-local registry can omit `type_base` and
let `Composition(..., errors=ErrorOptions(type_base=...))` supply it. Passing
`app=app` when creating the RPC contract uses those same resolved type URIs for
code generation. A standalone RPC contract can use a fully resolved registry.

Use `fastapi-canon generate --config fastapi-canon.toml` to export the app and its RPC contract
and generate one domain SDK with shared models and one error runtime. The
[common codegen guide](../../../docs/unified-canon-concept.md) and
[example configuration](../examples/canonical_errors/fastapi-canon.toml) show the complete
workflow. Specialized SSE/RPC generator commands remain available for separate
SDK outputs. Generated runtimes do not depend on Canon.

## What goes over the wire

| Occurrence | Binding | HTTP status in the problem |
| --- | --- | --- |
| HTTP or SSE admission failure | `application/problem+json` response | Actual response status |
| Error during SSE processing | terminal `event: canon.problem` | Omitted |
| Declared RPC domain failure | JSON-RPC `error.data.problem` | Omitted |

The JSON-RPC numeric error code and message remain in the normal envelope.
Canonical application errors use numeric server code `-32000`; the problem's
`type` distinguishes the application problem. Its extensions stay at the problem
root. The embedding is a Canon protocol binding of the RFC data model, rather
than a claim that RFC 9457 standardizes JSON-RPC or SSE.

Every normally exhausted Canon stream sends `canon.complete` with `{}`. A terminal
problem closes the stream without a completion frame. Undeclared processing
errors and failed public renderers are logged and produce a safe internal
problem. Cancellation and disconnect propagate through generator cleanup.
`canon.*` event names are reserved for the profile. Raw `StreamRouter` endpoints
keep their existing unprofiled SSE behavior. Binary RPC streams, raw socket
messages, and WebRTC media do not receive SSE control frames.

The SSE item schema describes business, problem, and completion frames. The
contract advertises version 1 in `x-canon-stream`, including its terminal problem
schemas and the business payload schema consumed by code generation. HTTP problem unions discriminate on resolved `type`.
The OpenRPC contract identifies canonical schemas with `x-canon-problem-schema`
and their stable URI with `x-canon-problem-type`.

## Handle failures in generated clients

The generated SDKs export `ProblemError`, `TransportError`, and `ProtocolError`.
`ProblemError.problem` carries public problem data and preserves unknown
extensions. Known RPC problem types also have generated error subclasses.
Python stream problems are validated against the operation's declared models.
TypeScript follows the generator's existing static model validation policy;
standard problem members and stream control messages are checked at runtime.

```typescript
import { createClient, ProblemError } from "./generated";

const client = createClient({ baseUrl: "https://api.example.com" });
try {
  const project = await client.projects.get({ projectId: 1 });
  for await (const event of client.projects.events({ projectId: 2 })) {
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

The generated stream iterator yields business events only. It consumes completion
frames, throws `ProblemError` on a terminal problem or HTTP problem response, and
throws `TransportError` on EOF without completion. Invalid control messages raise
`ProtocolError`. Terminal problems are not retried. Consumers using native
`EventSource` must close it after `canon.problem` or `canon.complete` to prevent
its automatic reconnection.

The same `ProblemError.problem` convention applies to generated RPC clients.
Unknown type URIs remain generic problems; matching a familiar `code` alone does
not turn them into a known generated subclass. The common workflow emits one shared error runtime and canonical model graph
inside the SDK. Specialized generators producing separate SDKs still have
separate runtime/class identities.

## Migration

Regenerate Python and TypeScript clients after upgrading. Catch `ProblemError`
for HTTP problem responses instead of inspecting `HttpError.details`;
`HttpError` remains the fallback for other HTTP error bodies. Update manual SSE
consumers to handle the two terminal events. Update HTTP schema consumers that
used the old `code` discriminator to use `type`. Give business-specific problems
distinct stable type URIs; ambiguous `about:blank` unions fail compilation.
