# Database

## Session ownership

The request scope owns the transaction. A repository participates in it but never finalizes it:

```python
@provide(scope=Scope.REQUEST)
async def session(factory: async_sessionmaker[AsyncSession]):
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
```

This permits several repositories to join one atomic use case. Call `flush()` inside a repository when an operation needs generated identifiers or constraint evaluation before returning.

For work whose lifetime is longer than a request, inject a session factory and create an explicit short transaction. Never retain a request session in background tasks, streams, or WebSocket state.

## Repository contracts

Put the contract in the domain and describe intent:

```python
class TaskRepository(ABC):
    @abstractmethod
    async def get_by_id(
        self,
        *,
        task_id: UUID,
        user_id: UUID,
    ) -> Task | None: ...

    @abstractmethod
    async def save(self, *, task: Task) -> Task: ...
```

Avoid exposing `filter_by(**kwargs)`, SQL expressions, sessions, or ORM models through the domain contract. Add a method when it represents a use-case-relevant operation.

## Generic SQL mechanics

A generic infrastructure base can safely centralize identical mechanics such as lookup by primary key, merge, delete, and existence checks. Bind both ORM and domain types, and require explicit mapping:

```python
class SqlRepository[ModelT: DatabaseModel, DomainT](ABC):
    def __init__(self, session: AsyncSession, model: type[ModelT]) -> None: ...

    @abstractmethod
    def to_domain(self, model: ModelT) -> DomainT: ...

    @abstractmethod
    def to_model(self, entity: DomainT) -> ModelT: ...
```

Feature repositories inherit the base and the domain contract. Specialized operations stay in the feature repository. Do not grow the base into a universal query language.

Import and inherit the feature's repository ABC in every SQL implementation. Do not rely on accidental structural compatibility: the explicit base class documents the adapter's role and lets static analysis catch missing abstract operations.

## SQLModel boundaries

Use `SQLModel` only for table mapping. Do not use a `table=True` model as a request schema or domain entity. Database nullability, generated defaults, relationship loading, and API optionality are separate concerns.

Prefer database-generated timestamps and identifiers where the database must be authoritative. Refresh after flush when returning those values. Use timezone-aware timestamp columns.

Load relationships deliberately. Repository methods must return fully usable domain values and must not rely on lazy loading after the session closes.

## Table placement and registration

Keep SQLModel table declarations together in `api.infrastructure.database.orm` while the model set remains reasonably small. One obvious file is easier to discover and gives Alembic a direct, explicit metadata import; do not distribute a handful of models across feature folders in anticipation of future growth.

Split only when the central file has become large enough to harm navigation, ownership, or feature cohesion. At that point, co-locate each table model with its feature's infrastructure code, for example `api.features.tasks.infrastructure.models`. Keep shared database primitives such as the base table type and naming convention in `api.infrastructure.database`.

Regardless of placement, give schema tooling one explicit registration point. With the default single-file layout, Alembic directly imports every table class from `orm.py`. Once models are distributed across features, add a small central registration function instead:

```python
# api/infrastructure/database/models.py
def register_models() -> None:
    # Imports register table classes in SQLModel.metadata.
    from api.features.tasks.infrastructure import models as task_models
    from api.features.users.infrastructure import models as user_models

    _ = (task_models, user_models)
```

Alembic and local bootstrap tools call this function before reading `SQLModel.metadata`. Application routers, repositories, or dependency providers must not be responsible for table registration as an accidental side effect. For the Alembic layout, see [Database migrations](migrations.md).

## Concurrency

Use database constraints as the final integrity boundary. For read-modify-write behavior, select with `FOR UPDATE` or use an atomic statement as appropriate. Handle expected uniqueness races explicitly, usually with a savepoint, without rolling back unrelated work in the outer transaction.

## Migrations

Alembic is authoritative for persistent schemas. Do not use `SQLModel.metadata.create_all()` as a production migration mechanism. For ownership and file placement, see [Database migrations](migrations.md).
