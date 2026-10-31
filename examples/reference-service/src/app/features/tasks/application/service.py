from uuid import UUID

from app.features.tasks.domain import Task, TaskRepository


class TaskNotFound(Exception):
    pass


class TaskService:
    def __init__(self, repository: TaskRepository) -> None:
        self._repository = repository

    async def create(self, *, user_id: UUID, title: str) -> Task:
        task = Task(user_id=user_id, title=title)
        return await self._repository.save(task=task)

    async def list_recent(self, *, user_id: UUID, limit: int = 50) -> list[Task]:
        return await self._repository.list_recent(user_id=user_id, limit=limit)

    async def complete(self, *, user_id: UUID, task_id: UUID) -> Task:
        task = await self._repository.get_by_id(task_id=task_id, user_id=user_id)
        if task is None:
            raise TaskNotFound(task_id)
        return await self._repository.save(task=task.complete())
