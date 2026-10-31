# fastapi-canon

`fastapi-canon` is an opinionated composition library for feature-oriented
FastAPI applications. A feature groups its routers, Dishka providers, error
contracts, exception handlers, and lifespan into one immutable value. The
application installs an explicitly ordered set of those values at its
composition root.

`CanonRouter` is the canonical API for declaring application routes. It adds
composable `raises=` and `response=` contracts to FastAPI's familiar route
decorators. Ordinary `APIRouter` declarations and the lower-level response
compilers remain supported for generated code and incremental migration.

Dishka is a deliberate part of this canon, not an optional integration.
`fastapi-canon` defines one dependency-injection approach: features contribute
Dishka providers, and the composition builds and owns one shared Dishka
container. Applications that choose another dependency-injection framework are
outside the library's intended architecture.

## Contents

- [Canon architecture and public API](../../docs/canon.md)
- [Unified package and migration](docs/package-migration.md)
- [Quick start](#quick-start)
- [Feature composition](#feature-composition)
  - [Shared API router](#shared-api-router)
  - [Router policies](#router-policies)
  - [Dishka](#dishka)
- [Error contracts](#error-contracts)
  - [Optional source checking](docs/error-checking.md)
  - [Canon routers](#canon-routers)
  - [OpenAPI representation](#openapi-representation)
- [Success response contracts](#success-response-contracts)
  - [Server-sent events](#server-sent-events)
  - [Binary and PDF streams](#binary-and-pdf-streams)
  - [Streaming boundary](#streaming-boundary)
- [Low-level FastAPI compatibility](#low-level-fastapi-compatibility)
- [Installation guarantees](#installation-guarantees)
- [Optional OpenTelemetry integration](docs/telemetry.md)
- [Requirements](#requirements)
- [Development](#development)
- [Showcase](#showcase)

## Quick start

The prepared 0.9.0 source includes SSE, WebSockets and WebRTC in the same wheel.
Install feature dependencies with `fastapi-canon[sse,websockets,webrtc]` and
generation tooling with `fastapi-canon[codegen]`. Use `canon sse`,
`canon websockets` and `canon webrtc` for contracts and clients.
See the [migration guide](docs/package-migration.md); this version is not yet
published.

```python
from fastapi import FastAPI
from fastapi_canon import CanonRouter, Composition, Feature

projects = CanonRouter(prefix="/projects", tags=["projects"])


@projects.get("")
async def list_projects() -> list[str]:
    return []


project_feature = Feature(name="projects", routers=[projects])

app = Composition(project_feature).apply(FastAPI())
```

## Feature composition

Every contribution is optional:

```python
feature = Feature(
    name="projects",
    routers=[router],
    providers=[provider],
    errors=feature_errors,
    exception_handlers=[handler_spec],
    lifespan=feature_lifespan,
)
```

When `errors=` is omitted, a feature collects the registries referenced by its
direct `CanonRouter` contributions. Supplying `errors=` remains available when
ownership must be explicit, such as a registry used by routers in another
feature. Repeated contributions of the same definitions are merged once at the
application boundary.

Mutable sequences passed to `Feature` are copied to tuples. Installing features
preserves declaration order for routes and startup. Shutdown runs in reverse
order, including cleanup of features that started before a later feature failed.
Feature names are required, unique within a composition, and included in
configuration diagnostics.

### Shared API router

Use `router_factory` to create one application router per installed composition.
Feature routers are included in that router before it is mounted on the app, so
the factory can supply a shared prefix, tags, and dependencies. Returning a
`CanonRouter` keeps the application boundary on the canonical router API:

```python
from fastapi import Depends
from fastapi_canon import CanonRouter


async def require_request_id() -> None: ...


composition = Composition(
    project_feature,
    router_factory=lambda: CanonRouter(
        prefix="/api/v1",
        tags=["api"],
        dependencies=[Depends(require_request_id)],
    ),
)
```

The factory is called during `apply()` and must return a fresh, empty
`APIRouter`; custom `APIRouter` subclasses remain supported. Omitting it
preserves direct router installation.

### Router policies

Use an immutable `RouterPolicy` to reuse HTTP prerequisites and the errors they
can raise while keeping `CanonRouter` explicit at each declaration:

```python
from dishka.integrations.fastapi import DishkaRoute
from fastapi import Depends
from fastapi_canon import CanonRouter, Composition, RouterPolicy

protected = RouterPolicy(
    error_registry=authentication_errors,
    dependencies=[Depends(require_authenticated_user)],
    raises=[authentication_required, forbidden],
)

projects = CanonRouter(
    prefix="/projects",
    tags=["projects"],
    route_class=DishkaRoute,
    policy=protected,
    error_registry=project_errors,
)


@projects.get("/{project_id}", raises=[project_not_found])
async def get_project(project_id: int) -> ProjectResponse: ...
```

The application supplies the authentication dependency and error definitions.
Dependencies execute through FastAPI; `raises` documents HTTP failures and does
not itself enforce authentication. Policies copy their input sequences to tuples.
A dependency-only policy needs no error registry. Policy errors must belong to
the policy's own registry. Router and operation errors may use the merged policy
and router catalog. The merge keeps the existing registry collision checks and
deduplicates identical Error instances. Policy errors precede router and local
errors; local declarations extend the shared contract. Policy dependencies run
before local router dependencies, with FastAPI's normal dependency caching.

To apply a policy across every feature router in a composition, including nested
routers, pass it at the composition boundary instead:

```python
composition = Composition(
    projects_feature,
    users_feature,
    router_policy=protected,
    router_factory=lambda: CanonRouter(prefix="/api/v1"),
)
```

`router_policy` merges its catalog with the feature registries and applies its
dependencies to the installed routes. Its error contracts extend the generated
HTTP operations, including typed SSE operations. Errors sharing a status produce
one combined Problem schema. A conflicting manual `responses=` declaration is
rejected before installation. A policy's `422` error replaces FastAPI's automatic
validation response just as a router-level `raises` declaration does.

Composition policies leave the original feature routers unchanged, so the same
features can be installed in another app with a different policy. Routes mounted
outside the composition do not acquire its prerequisites or declared errors.
Hidden routes execute the dependencies but have no OpenAPI operation to extend.
The application error registry still handles registered exceptions globally;
policy declarations are not a runtime exception allowlist.

`CanonRouter(policy=...)` shares error contracts with operations declared directly
on that router, like router-level `raises`. Use `Composition(router_policy=...)`
for contracts across included child routers; the meaning of ordinary `raises=`
and `include_router()` is unchanged. Keep `route_class=DishkaRoute` on the router
where endpoints are declared. A composition policy does not replace route classes
or retrofit injection. For WebRTC session admission contracts, use
`CanonWebRtcRouter(policy=...)`; composition HTTP contract compilation does not
extend the separate WebRTC session descriptor.

### Dishka

Dishka is a required runtime dependency and the canonical dependency-injection
mechanism. A feature accepts provider instances, provider classes, and
zero-argument factories:

```python
feature = Feature(
    name="projects",
    providers=[ProjectProvider, lambda: DatabaseProvider(settings.database)],
)
```

Classes and factories are materialized once per application during `apply()`;
instances are used as supplied. The providers from every feature are validated
together and used to build one `AsyncContainer`.

`Composition` manages the container lifecycle. It configures Dishka's FastAPI
middleware, exposes the container as `app.state.dishka_container`, and closes
it during application shutdown. Existing application lifespans are composed
automatically. Applications should treat the exposed container as
composition-owned and not close it independently.

## Error contracts

The optional [source checker](docs/error-checking.md) finds explicit application
exceptions missing from route `raises=` declarations, without importing your
application. Run `python -m fastapi_canon.check_error_contracts src`; unresolved calls and abstract
ports are reported as incomplete analysis. CI failure modes are opt-in.

Error contracts are implemented directly by `fastapi-canon`; no separate error
library is required. Each feature may expose one `ErrorRegistry`. Registries are
merged and installed once, so runtime RFC 9457 Problem Details and OpenAPI use
the same definitions:

```python
from fastapi_canon import (
    CanonRouter,
    Composition,
    Error,
    ErrorOptions,
    ErrorRegistry,
    Feature,
)


class ProjectNotFound(Exception):
    pass


project_not_found = Error(
    ProjectNotFound,
    status=404,
    code="project_not_found",
    title="Project not found",
    detail="The requested project does not exist.",
)

project_errors = ErrorRegistry(
    name="projects",
    errors=[project_not_found],
)

projects = CanonRouter(
    prefix="/projects",
    tags=["projects"],
    error_registry=project_errors,
)

project_feature = Feature(
    name="projects",
    routers=[projects],
)

app = Composition(
    project_feature,
    errors=ErrorOptions(
        type_base="https://api.example.com/problems",
    ),
).apply(FastAPI())
```

Declare operation failures from the same registry used at runtime:

```python
@projects.get(
    "/{project_id}",
    raises=[project_not_found],
)
async def get_project(project_id: str) -> dict[str, str]:
    raise ProjectNotFound(project_id)
```

Generic normalized `HTTPException` contracts remain available through the
low-level compatibility API described below. A domain error and a generic HTTP
problem may share a status; the generated response then uses the same
discriminated `oneOf` representation.

FastAPI `responses={...}` entries without canon error declarations require no
additional metadata. Success contracts and other media types can be declared
alongside problem responses.

#### OpenAPI representation

Every declared error becomes a reusable schema in `components.schemas`. The
corresponding operation references it as an `application/problem+json`
response:

```yaml
paths:
  /projects/{project_id}:
    get:
      responses:
        "404":
          description: Project not found
          content:
            application/problem+json:
              schema:
                $ref: "#/components/schemas/ProjectNotFoundProblem"

components:
  schemas:
    ProjectNotFoundProblem:
      type: object
      required: [type, title, status, code]
      properties:
        type:
          type: string
          const: https://api.example.com/problems/project_not_found
        title:
          type: string
          const: Project not found
        status:
          type: integer
          const: 404
        code:
          type: string
          const: project_not_found
        detail:
          type: [string, "null"]
```

Typed extension fields and documented response headers are added to that same
schema and response. If multiple errors share one status code, the response
uses `oneOf` with `code` as its discriminator. When validation normalization is
enabled, FastAPI's default `422` response is replaced by
`RequestValidationProblem` using the same media type.

When all local registries already share a `type_base`, it is inferred. The
`ErrorOptions` settings `include_validation_error`,
`include_http_exceptions`, and `include_unhandled_error` are passed to
the integrated error engine and default to `True`.

Use `ExceptionHandlerSpec` for a deliberately custom Starlette/FastAPI handler:

```python
from fastapi_canon import ExceptionHandlerSpec

feature = Feature(
    name="projects",
    exception_handlers=[ExceptionHandlerSpec(DomainError, domain_error_handler)],
)
```

`Error.code` is the stable client contract. Dynamic `detail`, extensions, and
headers are public response data and should contain only information classified
for API exposure. Exception text can be used as detail when it is itself a
reviewed public contract. Domain registries should use specific exception types;
broad built-ins such as `Exception`, `ValueError`, and `RuntimeError` can also
match unrelated programming failures.

### Canon routers

`CanonRouter` separates errors shared by a router context from the errors that
belong to one operation. Give the router its registry through
`error_registry=` and declare shared contracts once with `raises=`:

```python
from fastapi.responses import StreamingResponse
from fastapi_canon import CanonResponse, CanonRouter

router = CanonRouter(
    prefix="/sessions",
    error_registry=SESSION_ERRORS,
    raises=[
        AUTHENTICATION_REQUIRED_ERROR,
        SESSION_NOT_FOUND_ERROR,
        SESSION_ACCESS_DENIED_ERROR,
    ],
)


@router.post(
    "/{session_id}/playlist/spotify",
    raises=[
        SESSION_PLAYLIST_EMPTY_ERROR,
        SESSION_PLAYLIST_MISSING_SPOTIFY_TRACKS_ERROR,
        SESSION_EXPORT_REQUIRES_SPOTIFY_CONNECTION_ERROR,
    ],
    response=CanonResponse.sse(description="Server-sent session events."),
)
async def export_session_playlist_to_spotify(...) -> StreamingResponse: ...
```

Canon composes router-level and operation-level errors with the success response
and passes the compiled declaration through FastAPI's normal route API. It does
not patch `APIRouter` or `APIRoute`. Its documented `app.openapi` hook compiles
contracts from the generated OpenAPI document without traversing FastAPI's
internal route tree. The standard
`get`, `put`, `post`, `delete`, `options`, `head`, `patch`, and `trace`
decorators support `raises=` and `response=`; `api_route()` supports them for
custom method sets.

The error registry is optional for success-only routers, but any `raises=`
declaration requires one. Every declared error must belong to that exact
registry. The same error declared at both levels is included only once.

`response=` uses its own status as the route's `status_code` when none is given.
Canon rejects mismatched status codes, bodyless responses with an explicit
`response_model`, and status collisions with FastAPI's manual `responses=`
declarations at route declaration time. Other FastAPI decorator options continue
to pass through unchanged.

Application-registry membership and HTTP-normalization checks run when OpenAPI
is generated. Call `composition.validate(app)` after applying a composition, or
`validate_openapi_contracts(app)` for a manually assembled application, to make
that validation intent explicit. Compilation failures are not cached. Routes
excluded with `include_in_schema=False` are outside document-level validation;
`CanonRouter` still validates their explicit declarations when they are
registered. Router-level `raises=` applies to operations declared on that
router, not to separately included child routers.

For explicit method lists, import the string enum `HTTPMethod` from
`fastapi_canon`, for example `methods=[HTTPMethod.POST]`. Canon re-exports
Python's standard `http.HTTPMethod`; ordinary string method names also work.

### Typed SSE integration

Install `fastapi-canon[sse]` and import `CanonStreamRouter` from
`fastapi_canon.integrations.sse` to combine canon's shared/local `raises=`
with typed GET/POST SSE generators. `Feature` discovers its registry just as
for `CanonRouter`. The native SSE `itemSchema`/`contentSchema` is preserved for
both Streams client generators. Core imports do not load the SSE runtime; select `fastapi-canon[sse]` for its dependencies.

Run HTTP preconditions in dependencies: even generator code before the first
`yield` runs after the response starts. `raises=` documents HTTP problems;
business errors during a stream need an explicit error event, and connection
failures remain transport errors.

See the [integration API](docs/streams.md) for installation, router options,
composition, client generation, and the streaming boundary. The
[`examples/streams`](examples/streams) guide includes an executable GET/POST app
with Dishka injection in dependencies and generators, and local regression
commands.

## Success response contracts

`CanonResponse` describes one successful response directly on a `CanonRouter`
operation:

```python
from fastapi_canon import CanonResponse


@router.get(
    "/events",
    raises=[AUTHENTICATION_REQUIRED_ERROR, ACCESS_DENIED_ERROR],
    response=CanonResponse.sse(),
)
async def events() -> StreamingResponse: ...
```

These declarations are self-contained: a success-only `CanonRouter` produces
clean OpenAPI without installing an `ErrorRegistry` or another OpenAPI hook.

The generic form accepts a status, media type, OpenAPI schema, description, and
response headers:

```python
success = CanonResponse(
    status=200,
    media_type="application/example+json",
    schema={"type": "object"},
    description="Example document",
    headers=["X-Request-ID"],
)
```

The available constructors are:

| Constructor | Default contract |
| --- | --- |
| `CanonResponse.json()` | `application/json` with status 200 |
| `CanonResponse.empty()` | No response content with status 204 |
| `CanonResponse.stream(media_type)` | String stream with status 200 |
| `CanonResponse.binary(media_type)` | Binary string stream with status 200 |
| `CanonResponse.sse()` | `CanonResponse.stream("text/event-stream")` |

Success-only routers do not need an empty `ErrorRegistry`:

```python
@router.get(
    "/health",
    response=CanonResponse.json(
        schema={"type": "object"},
        description="Service health",
    ),
)
async def health() -> dict[str, str]: ...
```

The route declaration is independent of the application's error definitions.

Bodyless contracts also replace FastAPI's generated success content for 2xx and
3xx responses, so a redirect needs no matching `response_class` solely for
OpenAPI purposes:

```python
@router.get(
    "/elsewhere",
    response=CanonResponse.empty(status=307),
)
async def elsewhere() -> RedirectResponse: ...
```

A bodyless contract combined with an explicit `response_model` raises
`ResponseConfigurationError` at declaration time.

Schemas and header definitions are copied into immutable mappings. Header names
may be supplied as a list for standard string-valued header schemas or as a
mapping containing complete OpenAPI header definitions. `CanonRouter` derives
the route status from the success contract when `status_code` is omitted.

### Server-sent events

```python
from collections.abc import AsyncIterator

from fastapi.responses import StreamingResponse
from fastapi_canon import CanonResponse, CanonRouter


router = CanonRouter(
    error_registry=api_errors,
    raises=[AUTHENTICATION_REQUIRED_ERROR, ACCESS_DENIED_ERROR],
)


async def event_chunks() -> AsyncIterator[str]:
    yield "event: ready\ndata: {}\n\n"
    yield 'event: message\ndata: {"id": 1}\n\n'


@router.get(
    "/events",
    response=CanonResponse.sse(),
)
async def events() -> StreamingResponse:
    return StreamingResponse(
        event_chunks(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
```

### Binary and PDF streams

```python
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

from fastapi.responses import StreamingResponse
from fastapi_canon import CanonResponse, CanonRouter


router = CanonRouter(
    error_registry=document_errors,
    raises=[AUTHENTICATION_REQUIRED_ERROR, ACCESS_DENIED_ERROR],
)


def pdf_chunks(path: Path) -> Iterator[bytes]:
    with path.open("rb") as source:
        while chunk := source.read(64 * 1024):
            yield chunk


@router.get(
    "/documents/{document_id}",
    raises=[document_missing],
    response=CanonResponse.binary(
        "application/pdf",
        headers=["Content-Disposition"],
    ),
)
async def document(document_id: UUID) -> StreamingResponse:
    path = Path("documents") / f"{document_id}.pdf"
    if not path.is_file():
        raise DocumentMissing
    return StreamingResponse(
        pdf_chunks(path),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{document_id}.pdf"',
        },
    )
```

The generated operation documents only `text/event-stream` or
`application/pdf` for the successful response. Error responses continue to use
`application/problem+json`.

### Streaming boundary

Authentication, authorization, validation, and resource lookup should complete
before returning `StreamingResponse`. Once response headers have been sent, an
exception inside the iterator cannot be converted into another HTTP response.
SSE protocols can define an application-specific failure event; a failed binary
iterator produces an incomplete download.

## Low-level FastAPI compatibility

`CanonRouter` is the canonical API for new application code. The lower-level
compilers remain supported for ordinary `APIRouter`, generated routers, and
incremental migrations:

```python
from fastapi import APIRouter
from fastapi_canon import CanonResponse, install_openapi_contracts

legacy_router = APIRouter()


@legacy_router.get(
    "/private",
    responses=api_errors.responses(
        project_not_found,
        http_statuses=[401, 403],
        success=CanonResponse.json(),
    ),
)
async def private_project() -> dict[str, str]: ...


install_openapi_contracts(app)
```

`ErrorRegistry.responses()` still combines domain errors, normalized
`HTTPException` statuses, and an optional success contract.
`CanonResponse.responses()` still supports success-only declarations. Because
these primitives populate FastAPI's `responses={...}` mapping directly, they do
not provide all declaration-time consistency checks of `CanonRouter`. A
manually assembled application using low-level success contracts must call
`install_openapi_contracts(app)` before generating OpenAPI. `Composition`
installs that compiler automatically, including when no error registry exists.

## Installation guarantees

- Feature order is explicit and deterministic.
- Feature names identify conflicting contributions in diagnostics.
- Reinstalling the exact same feature objects with the same options is a no-op.
- A different second installation is rejected.
- Duplicate routers, providers, handlers, and error collisions fail during
  configuration.
- Registry and handler configuration is checked against a temporary application
  before installation. Operation contracts are checked during OpenAPI generation.
- Provider-backed features must be installed before the application starts.
- Disabling a feature means omitting it from `Composition`, which removes all of
  its contributions together.

Configuration failures raise `FeatureConfigurationError`.

## Requirements

Optional WebRTC sessions can declare the same admission errors with
[`CanonWebRtcRouter`](docs/webrtc.md) from `fastapi-canon[webrtc]`.

- CPython 3.12, 3.13, or 3.14
- FastAPI 0.115 or newer, below 1.0
- Dishka 1.10 or newer, below 2.0
- Pydantic 2.9 or newer, below 3.0

## Development

Run the shared development commands from the workspace root:

```console
uv sync --locked --all-packages --all-groups
uv run --locked pre-commit install
uv run --locked python scripts/check.py all
uv run --locked python scripts/check_distributions.py
```

## Showcase

See [`examples/showcase`](examples/showcase) for a runnable two-feature FastAPI
application. It keeps error contracts alongside their feature routes and merges
them once at the composition root.
