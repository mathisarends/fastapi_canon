# fastapi-canon

`fastapi-canon` is a small composition library for feature-oriented FastAPI
applications. A feature groups its routers, Dishka providers, error contracts,
exception handlers, and lifespan into one immutable value. The application
installs an explicitly ordered set of those values at its composition root.

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
    faults=feature_faults,
    exception_handlers=[handler_spec],
    lifespan=feature_lifespan,
)
```

Mutable sequences passed to `Feature` are copied to tuples. Installing features
preserves declaration order for routes and startup. Shutdown runs in reverse
order, including cleanup of features that started before a later feature failed.

### Dishka

Provider instances from every feature are validated together and used to build
one `AsyncContainer`. Applying the composition configures Dishka's FastAPI
middleware and closes the container during application shutdown. Dishka exposes
the container as `app.state.dishka_container`.

### Faults

Each feature may expose one `fastapi_faults.FaultRegistry`. The registries are
merged and installed once, so runtime Problem Details and OpenAPI use the same
definitions:

```python
from fastapi_canon import Composition, FaultOptions

app = Composition(
    project_feature,
    account_feature,
    faults=FaultOptions(
        type_base="https://api.example.com/problems",
    ),
).apply(FastAPI())
```

When all local registries already share a `type_base`, it is inferred. The
`FaultOptions` settings `include_validation_error`,
`include_http_exceptions`, and `include_unhandled_error` are passed to
`fastapi-faults` and default to `True`.

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
- Duplicate routers, providers, handlers, and fault collisions fail during
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
- fastapi-faults 0.1 or newer

## Development

Install the development dependencies and run the quality gates:

```console
uv sync --all-groups
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv build
```
