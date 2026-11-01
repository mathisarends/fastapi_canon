from fastapi_canon import Feature
from fastapi_canon.integrations.opentelemetry import instrument

from app.features.tasks.application import TaskNotFound, TaskService
from app.features.tasks.domain import TaskRepository
from app.features.tasks.infrastructure.provider import TaskProvider
from app.features.tasks.presentation.router import router

feature = Feature(
    name="tasks",
    routers=(router,),
    providers=(TaskProvider,),
    instrumentation=(
        instrument(TaskRepository),
        instrument(TaskService, expected=(TaskNotFound,)),
    ),
)
