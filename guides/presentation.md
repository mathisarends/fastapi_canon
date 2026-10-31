# Presentation

The presentation layer is the HTTP adapter. It owns FastAPI routers and dependencies, Pydantic transport schemas, HTTP mappings, cookies, and exception handlers. It does not query SQL or construct concrete infrastructure adapters.

For layer direction, see [Architecture](architecture.md). For practical examples, see the reference task [`router.py`](../examples/reference-service/src/app/features/tasks/presentation/router.py), [`schemas.py`](../examples/reference-service/src/app/features/tasks/presentation/schemas.py), and [`mapper.py`](../examples/reference-service/src/app/features/tasks/presentation/mapper.py).

## Routers

Keep endpoints linear: accept validated input, call application services, map the result, and return. Business decisions belong to domain/application code.

Assign service results to a named local before mapping them. Do not inline an
awaited service call inside a mapper or build collection response schemas in
the endpoint. Put collection conversion in a named presentation mapper:

```python
async def list_tasks(
    authenticated_user_id: AuthenticatedUserId,
    task_service: FromDishka[TaskService],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> TaskListResponse:
    tasks = await task_service.list_recent(
        user_id=authenticated_user_id,
        limit=limit,
    )
    return to_list_response(tasks)
```

Name injected instances after their role, such as `task_service`, rather than
using generic names such as `service`. An authenticated principal is use-case
input, not an unused guard parameter; pass it to every operation whose data is
user-owned.

Every route decorator must declare:

- a stable, globally unique `operation_id`;
- an explicit success `status_code`;
- a named `response_model` (or explicit `None` for a bodyless response);
- relevant non-success responses with named schemas; and
- a feature tag and router prefix.

Treat `operation_id` as a public client-generation identifier. Prefer predictable verb-and-resource names such as `create_task`, `list_tasks`, and `complete_task`. Do not derive it from Python function names implicitly or change it during internal refactors.

## Schemas

Define separate request and response models even when their fields currently
match: `CreateTaskRequest`, `TaskResponse`, and `TaskListResponse`. Use Canon
Problem Details contracts for errors rather than maintaining a second handwritten
error response model.

Do not use SQLModel table types or domain entities as FastAPI schemas. Do not expose untyped `dict` bodies. Prefer an explicit envelope schema for collections when pagination or metadata may evolve:

```python
class TaskListResponse(Schema):
    items: list[TaskResponse]
```

Centralize deliberate Pydantic defaults—alias behavior, unknown-field policy, and attribute parsing—in one presentation base. Add field bounds and descriptions where they form part of the API contract. Do not silently inherit database nullability as API optionality.

## Mapping

Map domain/application outputs explicitly to response schemas. Keep non-trivial mapping, including collection comprehensions, in a named mapper function rather than the endpoint or ORM adapter. This makes public API evolution independent from domain and storage changes.

For the corresponding persistence boundary, see [Infrastructure](infrastructure.md#mapping).

## Dependencies

Use `FromDishka` for application services. Use FastAPI `Depends` for HTTP-derived values such as authentication, cookies, headers, and request context. Framework dependency types do not cross into application methods.

For provider composition, see [Dependency Injection](dependency_injection.md). For the authenticated-principal pattern, see [Authentication](authentication.md#layer-boundaries).

## Errors

Declare known domain/application exceptions as `Error` values in the feature's
presentation layer. Collect them in an `ErrorRegistry`, give that registry to a
`CanonRouter`, and document errors with `raises=`. Shared authentication errors
belong on the router; resource-specific errors belong on the operation. A feature
discovers its direct Canon routers' registries, and `Composition` merges them and
installs runtime handlers and OpenAPI contracts once.

When several routers share HTTP prerequisites, keep their `CanonRouter`
declarations explicit and pass a reusable `RouterPolicy` through `policy=`.
Bundle the actual FastAPI dependencies with the errors they can raise. Use
`Composition(router_policy=...)` at the composition root when the policy must
cover all included feature routers, including nested routers. Local `raises=`
adds resource-specific errors. Keep `route_class=DishkaRoute` where endpoints
are declared; a composition policy does not retrofit endpoint injection. See
[router policies](../packages/fastapi-canon/README.md#router-policies).

```python
task_not_found = Error(
    TaskNotFound,
    status=404,
    code="task_not_found",
    title="Task not found",
    detail="The requested task does not exist.",
)
```

The public error media type is `application/problem+json`. Stable `code` values
and generated named Problem schemas are client contracts; exception strings are
not public detail unless deliberately reviewed. Include runtime headers and their
OpenAPI definitions together, such as `WWW-Authenticate` on authentication errors.
Use an explicit `ExceptionHandlerSpec` only for an intentionally custom handler.

Use `401` for absent/invalid authentication, `403` for denied authorization, `404` when a resource is absent or intentionally undisclosed, and `409` for a state conflict. Do not leak stack traces, credentials, database constraint text, or provider payloads.

Canon normalizes validation failures into `request_validation_error` Problem
Details by default. Configure the application's problem `type_base` at the
composition root and call `composition.validate(app)` before serving to check
the compiled operation contracts. Do not install a parallel validation handler
for the same error shape. See the reference service's
[error definitions](../examples/reference-service/src/app/features/tasks/presentation/errors.py)
and [runtime/contract tests](../examples/reference-service/tests/test_api.py).

For an optional source check of explicit application exceptions against route
`raises`, run `python -m fastapi_canon.check_error_contracts src`. This complements contract
validation and reports unsupported paths, including abstract ports, as incomplete.
See [error-contract checking](../packages/fastapi-canon/docs/error-checking.md) for
supported patterns and opt-in CI failure modes.
