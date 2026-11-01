from dishka import Provider, Scope, provide
from sqlalchemy.ext.asyncio import AsyncSession

from app.features.tasks.application import TaskService
from app.features.tasks.domain import TaskRepository
from app.features.tasks.infrastructure.repository import SqlTaskRepository


class TaskProvider(Provider):
    @provide(scope=Scope.REQUEST)
    def task_repository(self, session: AsyncSession) -> TaskRepository:
        return SqlTaskRepository(session)

    @provide(scope=Scope.REQUEST)
    def task_service(self, repository: TaskRepository) -> TaskService:
        return TaskService(repository)
