from dishka import Provider, Scope, provide
from fastapi_canon.integrations.opentelemetry import observe
from sqlalchemy.ext.asyncio import AsyncSession

from app.features.tasks.application import TaskService
from app.features.tasks.application.service import TaskNotFound
from app.features.tasks.domain import TaskRepository
from app.features.tasks.infrastructure.repository import SqlTaskRepository


class TaskProvider(Provider):
    repository_telemetry = observe(TaskRepository)
    service_telemetry = observe(TaskService, expected=(TaskNotFound,))

    @provide(scope=Scope.REQUEST)
    def task_repository(self, session: AsyncSession) -> TaskRepository:
        return SqlTaskRepository(session)

    @provide(scope=Scope.REQUEST)
    def task_service(self, repository: TaskRepository) -> TaskService:
        return TaskService(repository)
