# fastapi-canon

`fastapi-canon` is an opinionated composition library for feature-oriented
FastAPI applications. A feature groups its routers, Dishka providers, error
contracts, exception handlers, and lifespan into one immutable value. The
application installs an explicitly ordered set of those values at its
composition root.

Dishka is a deliberate part of this canon, not an optional integration.
`fastapi-canon` defines one dependency-injection approach: features contribute
Dishka providers, and the composition builds and owns one shared Dishka
container. Applications that choose another dependency-injection framework are
outside the library's intended architecture.

```python
from fastapi import APIRouter, FastAPI
from fastapi_canon import Composition, Feature

projects = APIRouter(prefix="/projects", tags=["projects"])


@projects.get("")
async def list_projects() -> list[str]:
    return []


project_feature = Feature(routers=[projects])

app = Composition(project_feature).apply(FastAPI())
```

## Contributions

Every contribution is optional:

```python
feature = Feature(
    routers=[router],
    providers=[provider],
    errors=feature_errors,
    exception_handlers=[handler_spec],
    lifespan=feature_lifespan,
)
```

Mutable sequences passed to `Feature` are copied to tuples. Installing features
preserves declaration order for routes and startup. Shutdown runs in reverse
order, including cleanup of features that started before a later feature failed.

### Dishka

Dishka is a required runtime dependency and the canonical dependency-injection
mechanism. Provider instances from every feature are validated together and
used to build one `AsyncContainer`. Applying the composition configures
Dishka's FastAPI middleware and closes the container during application
shutdown. Dishka exposes the container as `app.state.dishka_container`.

### Errors

Error contracts are implemented directly by `fastapi-canon`; no separate error
library is required. Each feature may expose one `ErrorRegistry`. Registries are
merged and installed once, so runtime RFC 9457 Problem Details and OpenAPI use
the same definitions:

```python
from fastapi_canon import Composition, Error, ErrorOptions, ErrorRegistry, Feature


class ProjectNotFound(Exception):
    pass


project_not_found = Error(
    ProjectNotFound,
    status=404,
    code="project_not_found",
    title="Project not found",
    detail=lambda error: str(error),
)

project_errors = ErrorRegistry(
    name="projects",
    errors=[project_not_found],
)

project_feature = Feature(
    routers=[projects],
    errors=project_errors,
)

app = Composition(
    project_feature,
    errors=ErrorOptions(
        type_base="https://api.example.com/problems",
    ),
).apply(FastAPI())
```

Declare endpoint responses from the same registry used at runtime:

```python
@projects.get(
    "/{project_id}",
    responses=project_errors.responses(project_not_found),
)
async def get_project(project_id: str) -> dict[str, str]:
    raise ProjectNotFound(project_id)
```

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
    exception_handlers=[ExceptionHandlerSpec(DomainError, domain_error_handler)],
)
```

## Installation guarantees

- Feature order is explicit and deterministic.
- Reinstalling the exact same feature objects with the same options is a no-op.
- A different second installation is rejected.
- Duplicate routers, providers, handlers, and error collisions fail during
  configuration.
- Known configuration errors are validated against a temporary application
  before the real application is changed.
- Provider-backed features must be installed before the application starts.
- Disabling a feature means omitting it from `Composition`, which removes all of
  its contributions together.

Configuration failures raise `FeatureConfigurationError`.

## Requirements

- CPython 3.12, 3.13, or 3.14
- FastAPI 0.115 or newer, below 1.0
- Dishka 1.10 or newer, below 2.0
- Pydantic 2.9 or newer, below 3.0

## Development

Install the development dependencies and run the quality gates:

```console
uv sync --all-groups
uv run pre-commit install --config .pre-commit-config.yml
uv run pre-commit run --config .pre-commit-config.yml --all-files
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv build
```

## Showcase

See [`examples/showcase`](examples/showcase) for a runnable two-feature FastAPI
application. It keeps error contracts alongside their feature routes and merges
them once at the composition root.
