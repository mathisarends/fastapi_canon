from abc import ABC, abstractmethod
from uuid import UUID

from app.features.tasks.domain.entities import Task


class TaskRepository(ABC):
    @abstractmethod
    async def get_by_id(self, *, task_id: UUID, user_id: UUID) -> Task | None: ...

    @abstractmethod
    async def list_recent(self, *, user_id: UUID, limit: int) -> list[Task]: ...

    @abstractmethod
    async def save(self, *, task: Task) -> Task: ...
