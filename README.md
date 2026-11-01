# fastapi-canon

The canonical FastAPI API for people and coding agents: declare features, typed
operations and public errors once, then generate the SDK your consumers use.
Canon gives recurring application problems a shared solution: feature wiring,
Dishka lifecycle, error mapping, streaming contracts and client boilerplate.

Keep business logic in ordinary Python. Canon carries its public contracts from
the server into OpenAPI/OpenRPC and generated Python and TypeScript clients.
The generated API follows your domain: `client.projects.get()`,
`client.projects.events()` and `client.projects.cancel()`.

## Contents

- [Core features](#core-features)
- [Install](#install)
- [Core API: routes, errors and composition](#core-api-routes-errors-and-composition)
- [Streams: typed events from server to SDK](#streams-typed-events-from-server-to-sdk)
- [WebSockets: RPC calls as domain methods](#websockets-rpc-calls-as-domain-methods)
- [Generate and use the SDK](#generate-and-use-the-sdk)
- [Check that errors are mapped and declared](#check-that-errors-are-mapped-and-declared)
- [Documentation](#documentation)
- [Repository](#repository)
- [Current releases](#current-releases)
- [Repository layout](#repository-layout)

## Core features

| Declare | Canon provides |
| --- | --- |
| `Feature` + `Composition` | Ordered router/provider installation, one Dishka container and managed startup/shutdown. |
| `Error` + `ErrorRegistry` + `raises=` | Shared public problem definitions for runtime responses, contracts and generated SDKs. |
| `CanonRouter` + `CanonResponse` | Familiar FastAPI routes with composable error and success contracts, including JSON, empty and binary responses. |
| `CanonStreamRouter` | Typed SSE events, declared terminal problems and explicit completion, consumed through async iteration. |
| `RpcChannel` + `RpcService` | Typed WebSocket RPC methods, notifications, subscriptions and declared socket/binary sessions. |
| `fastapi-canon generate` | One domain SDK per target, shared models/errors, internal protocol bindings and drift checks. |
| `fastapi-canon check errors` | Source analysis for missing error mappings/declarations, with explicit incomplete-analysis diagnostics. |
| Optional WebRTC and telemetry | Typed media-session contracts and explicit OpenTelemetry instrumentation at the composition boundary. |

The [public API guide](docs/canon.md) maps these responsibilities to imports.
The [application guides](guides/README.md) give humans and agents the same
conventions for feature slices, inward dependencies and framework-free domains.

## Install

```console
uv add fastapi-canon
uv add "fastapi-canon[sse,websockets]"
uv add --dev "fastapi-canon[codegen]"
uv run fastapi-canon --help
```

The base includes FastAPI, Pydantic and Dishka. All integrations live in the same
`fastapi-canon` distribution; extras select their dependencies. Add `webrtc`
for media sessions, `aiortc` for the reference media engine, or `otel` for
telemetry. See [installation and migration](docs/package-migration.md).
Python 3.12–3.14 is supported; see [compatibility](docs/compatibility.md).

## Core API: routes, errors and composition

Save this as `src/app.py`. A domain exception stays ordinary Python; its public
mapping drives both the HTTP response and the exported operation contract.

```python
from fastapi import FastAPI
from pydantic import BaseModel

from fastapi_canon import CanonRouter, Composition, Error, ErrorRegistry, Feature


class ProjectMissing(Exception):
    pass


class Project(BaseModel):
    id: int


project_missing = Error(
    ProjectMissing,
    status=404,
    code="project_missing",
    title="Project not found",
    detail="Choose another project.",
)
errors = ErrorRegistry(
    errors=[project_missing], type_base="https://api.example.com/problems"
)
http = CanonRouter(error_registry=errors)


def require_project(project_id: int) -> None:
    if project_id == 0:
        raise ProjectMissing()


@http.get(
    "/projects/{project_id}",
    operation_id="projects.get",
    response_model=Project,
    raises=[project_missing],
)
async def get_project(project_id: int) -> dict[str, int]:
    require_project(project_id)
    return {"id": project_id}


feature = Feature(name="projects", routers=[http])
composition = Composition(feature)
app = composition.apply(FastAPI())
composition.validate(app)
```

`GET /projects/1` returns `{"id": 1}`. `GET /projects/0` returns a 404
`application/problem+json` response with the stable type
`https://api.example.com/problems/project_missing` and code `project_missing`.
The same problem is documented in OpenAPI. An explicit `operation_id` gives
codegen the public SDK name `projects.get`; `response_model=Project` supplies its
typed result and validates the server's returned data.

A feature can also contribute Dishka `providers=`, a `lifespan=` and optional
`instrumentation=`. Composition owns installation and cleanup. Shared
`RouterPolicy` declarations reduce repeated authentication, dependencies and
error contracts. See the [core API guide](docs/composition.md),
[router policies](docs/composition.md#router-policies) and
[shared problem contracts](docs/problems.md).

## Streams: typed events from server to SDK

Add a stream to `src/app.py`, reusing the error registry and lookup above:

```python
from collections.abc import AsyncIterator

from fastapi import Depends
from fastapi_canon.integrations.sse import CanonStreamRouter

streams = CanonStreamRouter(error_registry=errors)


class ProjectEvent(BaseModel):
    sequence: int


@streams.stream(
    "/projects/{project_id}/events",
    operation_id="projects.events",
    dependencies=[Depends(require_project)],
    raises=[project_missing],
    stream_raises=[project_missing],
)
async def events(project_id: int) -> AsyncIterator[ProjectEvent]:
    yield ProjectEvent(sequence=1)
    if project_id == 2:
        raise ProjectMissing()
```

The event model becomes the generated iterator's item type.
`raises=` describes HTTP admission failures; `stream_raises=` describes failures
after streaming starts. Put admission checks in dependencies: generator bodies
run after the response begins.

The generated SDK handles SSE framing and JSON decoding, yields business events,
consumes `canon.complete`, and raises `ProblemError` on `canon.problem`.
Unexpected EOF raises `TransportError`; malformed control frames raise
`ProtocolError`. Consumers write an ordinary `async for` loop and can distinguish
completion from a broken connection. No application-specific SSE parser or
second error mapping is needed.

See [Canon stream contracts](docs/streams.md),
[stream clients](docs/integrations/sse/clients.md) and
[terminal problem behavior](docs/problems.md#what-goes-over-the-wire).

## WebSockets: RPC calls as domain methods

Add a WebSocket method using the same public error definition:

```python
from fastapi import APIRouter
from fastapi_canon.integrations.websockets import RpcChannel, RpcService
from fastapi_canon.integrations.websockets.adapters.fastapi import RpcRoutes

projects = RpcChannel("projects", error_registry=errors)


class CancelProject(BaseModel):
    project_id: int


@projects.method(raises=[project_missing])
async def cancel(params: CancelProject) -> None:
    require_project(params.project_id)


service = RpcService()
endpoint = service.socket("/ws", channels=[projects])
websockets = APIRouter()
RpcRoutes(websockets).mount(endpoint)
```

After all route declarations, replace the original composition block with this
one to install all three routers and expose the OpenRPC source for generation:

```python
feature = Feature(name="projects", routers=[http, streams, websockets])
composition = Composition(feature)
app = composition.apply(FastAPI())
composition.validate(app)
contract = service.contract(title="Projects", base_url="ws://localhost:8000", app=app)
```

Install a server and run the application from the project root:

```console
uv add --dev uvicorn
uv run uvicorn src.app:app --reload
```

The contract describes `projects.cancel` and its typed parameters and failures.
Codegen handles request IDs, serialization, response correlation and typed problem
errors; the consumer calls `client.projects.cancel(...)`. HTTP, SSE and RPC
share the SDK's public model/error runtime. Protocol bindings stay internal.

WebSockets also support notifications, subscriptions, typed sockets and binary
streams, with explicit session lifecycles. See [services and channels](docs/integrations/websockets/services.md),
[generated clients](docs/integrations/websockets/clients.md) and
[socket contracts](docs/integrations/websockets/sockets.md).

## Generate and use the SDK

Save `fastapi-canon.toml` at the project root. This configuration exports the app
above and generates both language targets from its HTTP/SSE and RPC contracts:

```toml
[codegen]
version = 1

[codegen.sources.api]
kind = "openapi"
source = "src.app:app"
output = "contracts/openapi.json"

[codegen.sources.control]
kind = "openrpc"
source = "src.app:contract"
output = "contracts/openrpc.json"

[codegen.targets.browser]
language = "typescript"
sources = ["api", "control"]
output = "frontend/generated"

[codegen.targets.python]
language = "python"
sources = ["api", "control"]
output = "api_client"
package = "api_client"
```

```console
uv run fastapi-canon generate
uv run fastapi-canon generate --check --diff
```

For the HTTP-only example, omit `sources.control` and use `sources = ["api"]`
on each target. A stream-only addition uses the same OpenAPI source as HTTP.
`--check --diff` checks generated files and exported contracts without writing;
use it in CI to catch drift. Offline generation can consume saved contracts
without importing application code.

The generated TypeScript API:

```typescript
import { createClient, ProblemError } from "./generated";

const client = createClient({ baseUrl: "http://localhost:8000" });
try {
  const project = await client.projects.get({ projectId: 1 });
  for await (const event of client.projects.events({ projectId: project.id })) {
    console.log(event.sequence);
  }
  await client.projects.cancel({ projectId: project.id });
} catch (error) {
  if (error instanceof ProblemError) console.error(error.problem.type);
  else throw error;
} finally {
  await client.close();
}
```

The same domain API in Python, inside an async function:

```python
from api_client import ApiClient, ProblemError

async with ApiClient("http://localhost:8000") as client:
    try:
        project = await client.projects.get(project_id=1)
        async for event in client.projects.events(project_id=project.id):
            print(event.sequence)
        await client.projects.cancel(project_id=project.id)
    except ProblemError as error:
        print(error.problem.type)
```

Generated SDKs do not depend on Canon. Python consumers install Pydantic, `httpx`
(or configured `httpx2`) and `websockets` for RPC; TypeScript uses fetch/DOM APIs
and the `ESNext.Disposable` type library. Bindings connect lazily, and closing the
client releases resources it owns. Shared semantic model identities avoid
duplicating DTOs across contracts.

The [codegen guide](docs/unified-canon-concept.md) covers naming, model identity,
connection/auth options, operation selection and output ownership. It also
documents limits: unified HTTP generation supports JSON and declared SSE;
unsupported multipart/file operations must be explicitly excluded. WebRTC
generation targets TypeScript. The [runnable canonical-errors example](examples/canonical_errors/app.py)
includes a complete multi-transport configuration.

## Check that errors are mapped and declared

An error contract is useful only if it describes the failures the code can raise.
Check the core example without importing or starting the application:

```console
uv run fastapi-canon check errors src --strict --require-complete
```

The checker follows supported endpoint, service, helper and dependency calls.
It compares escaping application exceptions with the registry and the combined
policy/router/operation `raises=` declarations:

| Finding | Meaning | Example fix |
| --- | --- | --- |
| `CAN001` | A mapped error is missing from `raises=`. | Restore `raises=[project_missing]` on `get_project`. |
| `CAN002` | An application exception has no registry mapping. | Define its public `Error` and add it to the router's registry. |
| `CAN003` | Analysis could not resolve a declaration or execution path. | Inspect the reported path and supported patterns. |

For example, remove `raises=[project_missing]` from the HTTP operation above:
the checker follows `get_project -> require_project` and reports `CAN001`.
`--strict` fails CI for mapping/declaration gaps; `--require-complete` also fails
for incomplete analysis or no supported operations. `--format json` produces a
machine-readable report for tooling and agents.

This checks supported explicit source paths, not every possible runtime failure.
Abstract Dishka ports and dynamic code can leave analysis incomplete. The checker
currently covers direct `CanonRouter` operations, not SSE/RPC/WebRTC handlers.
Keep runtime contract validation and behavioral tests. See
[error checking](docs/error-checking.md) for boundaries and
CI options.

## Documentation

| Goal | Start here |
| --- | --- |
| Build a feature with routes, providers and lifespans | [Core API and composition](docs/composition.md) |
| Choose canonical imports and ownership boundaries | [Public API](docs/canon.md) |
| Design an application humans and agents can extend | [Architecture](guides/architecture.md), [guide index](guides/README.md) |
| Map domain errors across HTTP, SSE and RPC | [Shared problems](docs/problems.md) |
| Check mapping/declaration coverage | [Error checking](docs/error-checking.md) |
| Generate Python and TypeScript domain SDKs | [Codegen workflow](docs/unified-canon-concept.md) |
| Build typed SSE streams | [Stream contracts](docs/streams.md), [SSE guide index](docs/integrations/sse/index.md) |
| Build WebSocket RPC, subscriptions or sockets | [WebSocket guide index](docs/integrations/websockets/index.md) |
| Add WebRTC sessions | [WebRTC contracts](docs/webrtc.md) |
| Add tracing and metrics | [Telemetry](docs/telemetry.md) |
| Upgrade an existing application | [Package migration](docs/package-migration.md), [RPC migration](docs/integrations/websockets/migration.md) |
| Run a complete application | [Reference service](examples/reference-service/README.md), [HTTP/SSE/RPC example](examples/stack_service.py) |
| Contribute or prepare a release | [Contributing](docs/contributing.md), [guardrails](docs/architecture-guardrails.md), [compatibility](docs/compatibility.md), [releases](docs/releases.md) |
| Understand broader design decisions | [Canonicalization record](CANONICALIZATION_DECISIONS.md) |

## Repository

The canonical home for development, issues and future releases is
[`mathisarends/fastapi_canon`](https://github.com/mathisarends/fastapi_canon).
The consolidated source returns from `mathisarends/canon` with its complete Git
history. The PyPI project remains `fastapi-canon`, and Python imports remain
`fastapi_canon`. There is one maintained source repository, not a mirrored pair.
See the [repository migration](docs/repository-migration.md) for the cutover
sequence, publisher setup and historical release locations.

## Current releases

Canon **0.11.0** is the current published unified distribution.
It replaces `observe()` with feature-owned
`instrument()` declarations and fixes generated Python RPC clients. See the
[migration notes](CHANGELOG.md#0110---2026-11-01)
and [publication record](docs/releases.md#current-versions).
The other names below are historical releases from the earlier four-distribution
layout; use Canon's feature extras for current development.

| Package | Version | GitHub | PyPI |
| --- | --- | --- | --- |
| `fastapi-canon` | 0.11.0 | [Release notes and artifacts](https://github.com/mathisarends/fastapi_canon/releases/tag/fastapi-canon-v0.11.0) | [Install](https://pypi.org/project/fastapi-canon/0.11.0/) |
| `fastapi-streams` | 0.5.0 | [Release notes and artifacts](https://github.com/mathisarends/canon/releases/tag/fastapi-streams-v0.5.0) | [Install](https://pypi.org/project/fastapi-streams/0.5.0/) |
| `fastapi-rpckit` | 0.4.1 | [Release notes and artifacts](https://github.com/mathisarends/canon/releases/tag/fastapi-rpckit-v0.4.1) | [Install](https://pypi.org/project/fastapi-rpckit/0.4.1/) |
| `fastapi-webrtc` | 0.3.0 | [Release notes and artifacts](https://github.com/mathisarends/canon/releases/tag/fastapi-webrtc-v0.3.0) | [Install](https://pypi.org/project/fastapi-webrtc/0.3.0/) |

Canon releases use this repository's
[release list](https://github.com/mathisarends/fastapi_canon/releases), one Canon
version, tag, changelog and artifact set. Historical release URLs and assets stay
in the former [`canon` release list](https://github.com/mathisarends/canon/releases)
and their original repositories.

## Repository layout

This repository builds one distribution, `fastapi-canon`, directly from its root
`pyproject.toml`. The source package is `fastapi_canon/`; all optional integrations
ship in that same wheel. Development uses one root Python environment and one
Node toolchain, without a package workspace.

| Location | Contents |
| --- | --- |
| [`fastapi_canon/`](fastapi_canon) | Composition, HTTP contracts, optional integrations and generators |
| [`tests/`](tests) | Separate unit, integration and contract roots; fixtures, TypeScript and browser suites |
| [`examples/`](examples) | Runnable applications and their generated clients, including the reference service |
| [`docs/`](docs) | Library, transport and contributor documentation; predecessor history |
| [`guides/`](guides) | Architecture conventions for consuming applications |
| [`scripts/`](scripts), [`tooling/`](tooling) | Development/release entrypoints and scoped analyzer configurations |

See [repository architecture](docs/architecture.md) for ownership and dependency
rules, [contributing](docs/contributing.md) for root commands,
[compatibility](docs/compatibility.md) for tested scope and
[releases](docs/releases.md) for artifact verification and publication.

The imported repositories remain available in Git history. Current development
and future releases use `mathisarends/fastapi_canon`; historical releases and
predecessor repositories are preserved.
