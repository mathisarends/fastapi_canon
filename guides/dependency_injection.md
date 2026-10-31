# Dependency Injection

Use dependency injection to make object construction, lifetimes, and adapter selection explicit. It is a composition technique, not a service-locator API for business code.

## Constructor injection

Application services declare collaborators in their constructors and depend on inward-facing ABCs:

```python
class TaskService:
    def __init__(self, repository: TaskRepository) -> None:
        self._repository = repository
```

Do not construct infrastructure adapters in services, pass a container into them, or resolve dependencies dynamically. Explicit constructors keep use cases framework-independent and reveal dependency growth.

Use the project's [ABC convention](architecture.md#abcs-for-owned-ports) for interfaces it owns. Providers return the ABC while constructing a concrete implementation:

```python
@provide(scope=Scope.REQUEST)
def repository(session: AsyncSession) -> TaskRepository:
    return SqlTaskRepository(session)
```

## Dishka and FastAPI responsibilities

Use Dishka for application dependencies: repositories, services, settings, database resources, and external adapters. Use FastAPI `Depends` for values or behavior derived from the HTTP connection: authentication credentials, headers, cookies, query policy, and request context.

```python
AuthenticatedUserId = Annotated[UUID, Depends(authenticated_user_id)]

async def endpoint(
    user_id: AuthenticatedUserId,
    task_service: FromDishka[TaskService],
) -> TaskResponse: ...
```

FastAPI and Dishka types stop at the presentation layer. Pass plain values and application types to services. Name injected instances after their role, such as `task_service`, so the local namespace remains meaningful.

## Composition root

Compose the FastAPI application in one root module with `fastapi-canon`. That
module may know concrete implementations; domain and application modules may not.

Represent each feature's integration surface as data:

```python
from fastapi import FastAPI
from fastapi_canon import CanonRouter, Composition, ErrorOptions, Feature

database = Feature(name="database", providers=[DatabaseProvider])

app = Composition(
    database,
    authentication_feature,
    tasks_feature,
    router_factory=lambda: CanonRouter(prefix="/api/v1"),
    errors=ErrorOptions(type_base="https://api.example.test/problems"),
).apply(FastAPI())
```

Features contribute routers, provider classes/instances/factories, error
registries, and optional lifespan functions. Use the library's `Feature` rather
than defining a local copy. `Composition` validates and assembles these explicit
contributions, creates one application Dishka container, and owns its shutdown.
Feature startup follows declaration order; teardown runs in reverse order.

Keep FastAPI's own connection dependencies at the boundary, including an
`@inject` authentication dependency receiving a `FromDishka[TokenVerifier]`.
Services receive the resulting plain principal or user ID. Do not construct or
close another Dishka container in feature routers.

The composition root can register public operational endpoints such as `/health`
outside its versioned API router. See the runnable
[reference root](../examples/reference-service/src/app/main.py). For typed SSE
and WebSocket contributions sharing the same container, see the
[stack example](../examples/stack_service.py).

Avoid import-time network connections and database work. Container construction may describe resources, but providers acquire them within managed scopes.
