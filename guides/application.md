# Application

The application layer implements use cases. It coordinates domain behavior through owned ports without knowing FastAPI, Dishka, SQLModel, HTTP, or a concrete database.

For the surrounding dependency rules, see [Architecture](architecture.md). For a practical implementation, see the reference [`TaskService`](../examples/reference-service/src/app/features/tasks/application/service.py).

## Services and use cases

Use constructor injection and type dependencies by their ABCs. A method represents a use case or cohesive application operation, not a CRUD mirror generated from table columns.

Accept plain application/domain values. Transport validation and extraction happen in presentation; persistence mapping happens in infrastructure.

```python
class TaskService:
    def __init__(self, repository: TaskRepository) -> None:
        self._repository = repository

    async def create(self, *, user_id: UUID, title: str) -> Task:
        task = Task(user_id=user_id, title=title)
        return await self._repository.save(task=task)
```

Create a domain entity or value object on its own named line before passing it to a repository, gateway, event, or nested constructor. Avoid `save(task=Task(...))`: the local name exposes an important state transition, is easier to inspect in a debugger, and leaves room for validation or events without rewriting the call.

Keep keyword-only use-case parameters when several values have the same primitive type or when call-site meaning benefits from labels.

Pass the authenticated actor into application methods that operate on
user-owned data. Repository reads for owned resources include that identity in
their contract and query, rather than loading globally and relying on the
presentation layer to have authenticated someone.

## Orchestration

Let domain objects enforce their own invariants. The application service loads required state, invokes domain behavior, coordinates ports, and returns a domain value or explicit application result.

Do not commit database transactions in a service or repository. The request/task scope owns the transaction so one use case can coordinate multiple repositories atomically. For transaction details, see [Database](database.md#session-ownership).

Avoid returning tuples or untyped dictionaries. Define a frozen result dataclass when a use case returns a projection that is not a domain entity.

## Exceptions

Raise domain or application exceptions with domain identifiers and sanitized context. Do not raise `HTTPException` or choose status codes here. Presentation maps known exceptions to complete HTTP error schemas; see [Presentation](presentation.md#errors).

Use `None` only where absence is an expected repository result. Convert unexpected absence into a use-case-specific exception before returning to presentation.

## Input models

Do not pass Pydantic request schemas into application services. Prefer explicit arguments for small commands. Use a plain frozen dataclass for a larger command or query whose grouping has application meaning.

Pydantic may be used inside application code only when validation/serialization is itself an application boundary and the choice is deliberate; it must not couple the layer to an HTTP schema.
