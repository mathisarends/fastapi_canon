# SSE routes and HTTP contracts

Select the SSE dependencies:

```console
pip install "fastapi-canon[sse]"
```

Import the router explicitly:

```python
from fastapi_canon.integrations.sse import CanonStreamRouter
```

The prepared Canon 0.9.0 release includes the SSE implementation in the same
wheel. The `sse` extra selects FastAPI >=0.143.3 and Pydantic >=2.11.2;
base composition keeps its existing lower bounds. No separate Streams package
is installed. See the [migration guide](package-migration.md).

## Complete integration example

Save this as `app.py` and run `uvicorn app:app` with Uvicorn installed:

```python
from collections.abc import AsyncIterator
from typing import Annotated

from dishka import FromDishka, Provider, Scope, provide
from dishka.integrations.fastapi import inject
from fastapi import Depends, FastAPI, Header
from pydantic import BaseModel

from fastapi_canon import HTTPMethod, Composition, Error, ErrorRegistry, Feature
from fastapi_canon.integrations.sse import CanonStreamRouter


class AuthenticationRequired(Exception):
    pass


class RunNotFound(Exception):
    pass


class RunEvent(BaseModel):
    run_id: str
    message: str


class RunService:
    async def require_existing(self, run_id: str) -> None:
        if run_id != "demo":
            raise RunNotFound

    async def events(self, run_id: str) -> AsyncIterator[RunEvent]:
        yield RunEvent(run_id=run_id, message="Started")
        yield RunEvent(run_id=run_id, message="Completed")


class RunProvider(Provider):
    @provide(scope=Scope.REQUEST)
    def run_service(self) -> RunService:
        return RunService()


authentication_required = Error(
    AuthenticationRequired,
    status=401,
    code="authentication_required",
    title="Authentication required",
)
run_not_found = Error(
    RunNotFound, status=404, code="run_not_found", title="Run not found"
)
run_errors = ErrorRegistry(
    name="runs",
    errors=[authentication_required, run_not_found],
    type_base="https://api.example.com/problems",
)


async def require_authentication(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    if authorization != "Bearer demo":
        raise AuthenticationRequired


@inject
async def require_existing_run(run_id: str, runs: FromDishka[RunService]) -> None:
    await runs.require_existing(run_id)


router = CanonStreamRouter(
    prefix="/runs",
    error_registry=run_errors,
    raises=[authentication_required],
    dependencies=[Depends(require_authentication)],
)


@router.stream(
    "/{run_id}/events",
    methods=[HTTPMethod.GET],
    raises=[run_not_found],
    dependencies=[Depends(require_existing_run)],
    operation_id="run_events",
)
@inject
async def events(run_id: str, runs: FromDishka[RunService]) -> AsyncIterator[RunEvent]:
    async for event in runs.events(run_id):
        yield event


feature = Feature(name="runs", routers=[router], providers=[RunProvider])
composition = Composition(feature)
app = composition.apply(FastAPI())
composition.validate(app)
```

Request `GET /runs/demo/events` with `Authorization: Bearer demo` to receive SSE.
Omit the header for a 401 problem, or request another run ID for a 404 problem.
`Feature` automatically collects the registry from the router; declaring
`errors=run_errors` on the feature is optional. `Composition` installs the
runtime handlers and OpenAPI compiler together.

## Dishka injection

`Feature(providers=[RunProvider])` lets `Composition` build the Dishka container,
install its FastAPI middleware, and manage shutdown. No separate
`make_async_container()` or `setup_dishka()` call is needed. Dishka is already
a canon dependency.

The example injects `RunService` with `FromDishka[RunService]` in both the
`Depends` precondition and the stream generator. `Scope.REQUEST` gives both
functions the same service instance for that HTTP request, and keeps it alive
until the stream finishes. A new request gets a new instance.

Place `@inject` directly above the function, below `@router.stream(...)`.
Decorate the dependency function with `@inject` too. Run the resource check in
`require_existing_run`: injecting the generator itself does not move checks
or provider creation into the pre-response dependency phase. Resolve any
service that must fail as an HTTP problem in a dependency first. See
[Dishka's FastAPI integration](https://dishka.readthedocs.io/en/stable/integrations/fastapi.html)
for `FromDishka` and dependency injection details.

## Router and operation options

`CanonStreamRouter` inherits `CanonRouter` and `StreamRouter`. It combines their
existing APIs without replacing registration, validation, or serialization.

| API | Behavior |
| --- | --- |
| `CanonStreamRouter(error_registry=registry, raises=[...], **options)` | The registry contains available errors. Router-level `raises` applies to operations declared on this router. Standard FastAPI router options pass through. |
| `router.stream(path, raises=[...], methods=None, **options)` | Local errors extend shared errors. Defaults to GET; use `methods=[HTTPMethod.POST]` for a body-driven stream. FastAPI dependencies, tags, operation IDs, and manual responses pass through. |
| `router.get(...)`, `router.post(...)`, `router.api_route(...)` | Normal canon REST declarations remain available on the same router. |

### HTTP methods

`HTTPMethod` is available from `fastapi_canon`. It re-exports Python's standard
`http.HTTPMethod`, a string enum with `GET`, `POST`, `PUT`, `PATCH`, `DELETE`,
`HEAD`, `OPTIONS`, `TRACE`, and `CONNECT`. It works in normal FastAPI/canon
`methods=` lists too; strings remain supported.

For a separate POST operation, add this before constructing the feature/app:

```python
from fastapi_canon import HTTPMethod


@router.stream(
    "/{run_id}/events",
    methods=[HTTPMethod.POST],
    raises=[run_not_found],
    dependencies=[Depends(require_existing_run)],
    operation_id="create_run_events",
)
@inject
async def create_events(
    run_id: str, runs: FromDishka[RunService]
) -> AsyncIterator[RunEvent]:
    async for event in runs.events(run_id):
        yield event
```

`methods=[HTTPMethod.POST]` selects **only POST**. The list is the native Streams
API, following FastAPI's `api_route()` convention. It can also select both
`[HTTPMethod.GET, HTTPMethod.POST]` for the same handler. Streams currently
accepts GET and POST only; other enum members remain invalid for `stream()`.
GET is useful for subscribing to events; POST lets the request include a typed
JSON body, as in [`examples/streams/app.py`](../examples/streams/app.py).
For generated clients, declare GET and POST separately with different
`operation_id` values, as the runnable example does.

Every declared error must be an `Error` instance in that router's registry.
Nonempty `raises` requires a registry. Duplicate instances, including errors
declared at both levels, appear once. Distinct errors with the same HTTP status
use canon's discriminated `oneOf` problem schema. Invalid declarations raise
`ErrorConfigurationError` during router construction or endpoint registration.
As with `CanonRouter`, shared `raises` does not propagate to included child routers.

Use an annotated generator (`AsyncIterator[RunEvent]`, or `Iterator[RunEvent]`
for synchronous endpoints). Streams infers its successful SSE response and
rejects incompatible methods, response models, and serialization options. No
`CanonResponse.sse()` declaration is needed. Typed POST bodies and a
discriminated event union are shown in the executable
[`examples/streams/app.py`](../examples/streams/app.py).

To document normalized `HTTPException` statuses, use canon's existing
`responses=run_errors.responses(http_statuses=[403])` router/operation option.
Request validation errors are normalized and documented by `Composition` by
default. For manual app assembly, include the router and call
`run_errors.install(app)` before generating OpenAPI. If you bypass `Composition`,
also configure Dishka's container, middleware, and lifespan yourself.

## Export and client generation

Canon preserves Streams' complete native `itemSchema` and JSON `contentSchema`,
including referenced models and event discriminators. The exported contract
also contains the declared `application/problem+json` responses. From this repo:

```console
uv sync --locked --all-packages --all-groups
uv run --locked canon sse schema --app examples.streams.app:app --output openapi.json
uv run --locked canon sse generate --schema openapi.json --language python --output generated/python
uv run --locked canon sse generate --schema openapi.json --language typescript --output generated/typescript
```

Another application can install `fastapi-canon[codegen]` and use its own
module path.
Both clients consume the same OpenAPI 3.2 contract.

## Streaming boundary

- **Before response start:** authentication, authorization, and resource checks
  in dependencies can raise domain exceptions. Canon returns RFC 9457 HTTP
  problems, including normalized HTTP and request validation errors.
- **During streaming:** every generator statement, even before its first
  `yield`, executes after response start. Business errors need an explicit
  application error event in the event model/union.
- **Connection failures:** disconnects remain transport errors.

`raises=` documents HTTP contracts. It does not move generator execution, catch
stream exceptions, or convert them to events. There is no `stream_raises` API.

## Compatibility regression checks

The [root CI workflow](../../../.github/workflows/ci.yml) checks the locked workspace on Linux with
Python 3.12–3.14 and Windows with Python 3.14. Regression coverage includes
dependency problems, typed GET/POST streams, complete SSE schemas, both client
generators and Dishka lifetime/cleanup. Installed-wheel checks also exercise
Canon with Streams absent. Run the focused suite from the repository root:

```console
uv run --locked --directory packages/fastapi-canon python -m pytest tests/integrations tests/test_optional_streams.py
```
