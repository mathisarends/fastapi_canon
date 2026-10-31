# Architecture

## Feature slices

Prefer feature ownership over global technical folders:

```text
src/api/
├── features/
│   └── tasks/
│       ├── domain/
│       ├── application/
│       ├── infrastructure/
│       ├── presentation/
│       └── feature.py
├── infrastructure/
│   └── database/
├── presentation/
└── main.py
```

In a workspace, the import package matches its member (`api/src/api`, `worker/src/worker`). See [uv workspace](uv-workspace.md) for the repository conventions.

Shared folders contain mechanisms used by multiple features, never a miscellaneous collection of business helpers. A feature may omit a layer until it needs it.

Package boundaries and public facades are defined in [Imports and Re-exports](imports-and-reexports.md).

## Dependency direction

```text
presentation ──> application ──> domain
infrastructure ─────────────────> domain
composition root ──> every outer layer
```

The domain owns ports such as `TaskRepository`. Infrastructure implements them. The application accepts the port by constructor injection. This keeps the use case independent of FastAPI and the database.

Do not place an ORM-independent repository contract in infrastructure: doing so reverses the dependency and makes the application depend on an adapter.

## ABCs for owned ports

Define application-owned interfaces as nominal abstract base classes:

```python
# domain/repository.py
class TaskRepository(ABC):
    @abstractmethod
    async def get_by_id(
        self,
        *,
        task_id: UUID,
        user_id: UUID,
    ) -> Task | None: ...


# infrastructure/repository.py
from api.features.tasks.domain.repository import TaskRepository


class SqlTaskRepository(SqlRepository[TaskModel, Task], TaskRepository):
    async def get_by_id(
        self,
        *,
        task_id: UUID,
        user_id: UUID,
    ) -> Task | None: ...
```

The concrete class must import and inherit the ABC even when it would happen to satisfy the same shape without inheritance. This makes the intended contract explicit, prevents instantiation while abstract methods are missing, and gives language servers a navigable implementation/definition relationship.

Prefer an ABC when this codebase owns the interface and implementations are expected to declare membership. Use `Protocol` when structural compatibility is itself the goal—for example, adapting a third-party object that cannot inherit the application's type, or defining a caller-owned shape across a boundary. Do not use `Protocol` merely to avoid the explicit import from an infrastructure implementation to its inward-facing domain port; that dependency is intentional.

## Layer responsibilities

### Domain

- Entities, value objects, policies, and domain exceptions.
- Repository and gateway contracts expressed in domain terms.
- No framework types or serialization concerns.

For entity, aggregate, and value-object conventions, see [Domain](domain.md).

### Application

- Use-case orchestration and transaction-neutral business workflows.
- Constructor-injected domain ports.
- Result records where returning a full entity is inappropriate.

For detailed rules and practical examples, see [Application](application.md).

### Infrastructure

- SQL repositories, external clients, storage adapters, and provider wiring.
- Mapping between external representations and domain types.
- No HTTP status or response-shape decisions.

For detailed rules and practical examples, see [Infrastructure](infrastructure.md).

### Presentation

- FastAPI routers, HTTP dependencies, Pydantic request/response schemas, mappers, and exception handlers.
- No SQL queries and no construction of concrete repositories.

For detailed rules and practical examples, see [Presentation](presentation.md).

Dependency construction and feature registration are defined in [Dependency Injection](dependency_injection.md).

## Mapping boundaries

Use explicit functions for non-trivial mappings. They make changes to database storage and public API shape independent:

```text
TaskModel <── repository mapper ──> Task <── presentation mapper ──> TaskResponse
```

Avoid relying on Pydantic `from_attributes` as an implicit domain-to-API contract for nested or evolving models.
