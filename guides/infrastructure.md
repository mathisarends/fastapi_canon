# Infrastructure

The infrastructure layer implements application/domain ports using SQL, external APIs, queues, object storage, cryptography, and other technical systems. It depends inward on owned ABCs; inward layers never import its implementations.

For layer direction and the ABC preference, see [Architecture](architecture.md). For practical code, see the reference [`SqlTaskRepository`](../examples/reference-service/src/app/features/tasks/infrastructure/repository.py) and [`TaskProvider`](../examples/reference-service/src/app/features/tasks/infrastructure/provider.py).

## Adapters

An adapter explicitly imports and inherits the port it implements:

```python
class SqlTaskRepository(SqlRepository[TaskModel, Task], TaskRepository): ...
```

Do not rely on structural coincidence. Keep provider/client-specific exceptions inside the adapter; translate expected failures into application/domain exceptions or results that the port defines.

Keep adapters focused. A SQL repository owns persistence for an aggregate or coherent projection. An OAuth client talks to the provider. A storage adapter stores objects. Do not create one infrastructure service that mixes unrelated systems.

## Mapping

Convert external types into domain values before returning through a port. A repository must not return ORM models, rows, or lazy relationships. An external client must not leak vendor SDK response objects.

Use explicit `to_domain` and `to_model` functions/methods. Ensure returned domain objects are usable after the session or client context closes. For generic SQL mechanics, see [Database](database.md#generic-sql-mechanics).

## Providers and resources

Provider functions are the only place that select a concrete adapter for a port. Scope repositories with their request/session resource. Scope engines, pools, settings, and safe stateless clients to the application.

Resource providers acquire and release connections/clients with `yield`. Long-running jobs and streams receive a factory or their own scope; they never capture an HTTP request session.

## Configuration

Define typed `BaseSettings` per infrastructure concern and inject them. Do not instantiate settings repeatedly across adapters or read environment variables inside business methods.

Keep secret values out of representations and logs. Validate required settings at startup where possible. Construct settings at the composition boundary instead of depending on ambient reads throughout the application.

## Resilience and observability

Set timeouts at adapter boundaries and retry only transient, idempotent
operations. Never log secrets or tokens. Preserve exception chaining when
translating infrastructure failures.

The Alembic layout is described in [Database migrations](migrations.md). The container file layout is shown in [Docker](docker.md).
