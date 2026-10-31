# Domain

The domain models business identity, state, and rules with ordinary Python
classes. It does not depend on FastAPI, Pydantic, Dishka, SQLAlchemy, or
SQLModel.

## Entities and aggregates

Use a small shared `Entity` base for identity and creation metadata that are
truly common across features. Keep feature state and behavior on a normal
class rather than using a dataclass for domain entities. Dataclasses remain a
good fit for value objects and application result records.

```python
from datetime import UTC, datetime
from uuid import UUID, uuid4


class Entity:
    def __init__(
        self,
        id: UUID | None = None,
        created_time: datetime | None = None,
    ) -> None:
        self._id = id or uuid4()
        self._created_at = created_time or datetime.now(UTC)

    @property
    def id(self) -> UUID:
        return self._id

    @property
    def created_at(self) -> datetime:
        return self._created_at


# Semantic sugar
Aggregate = Entity
```

`Aggregate` documents that an entity is a consistency and persistence
boundary; it does not need different mechanics merely to carry that meaning.
Add shared behavior only when all entities genuinely require it.

Feature aggregates inherit the alias, initialize their state explicitly, and
expose named methods for state transitions:

```python
class Task(Aggregate):
    def __init__(
        self,
        title: str,
        user_id: UUID,
        completed: bool = False,
        id: UUID | None = None,
        created_time: datetime | None = None,
    ) -> None:
        super().__init__(id=id, created_time=created_time)
        self._title = title
        self._user_id = user_id
        self._completed = completed

    @property
    def title(self) -> str:
        return self._title

    @property
    def user_id(self) -> UUID:
        return self._user_id

    @property
    def completed(self) -> bool:
        return self._completed

    def complete(self) -> Self:
        self._completed = True
        return self
```

Keep entity state private by convention. Expose only attributes that consumers
need to read, using getter-only properties for read-only access. Change
protected state through named domain methods so the aggregate retains control
of its invariants. Use a public attribute only when unrestricted direct read
and write access is intentionally part of the domain model; do not add a
property setter merely for convenience.

Do not let callers replace identity or creation metadata during a state
transition. Repositories reconstruct persisted aggregates through the
constructor and persist aggregates only at their boundary.

## Domain values

Use frozen dataclasses for immutable values without identity when generated
equality and representation are useful. Do not introduce entity identity into
a value object.
