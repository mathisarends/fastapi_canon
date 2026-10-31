from fastapi_canon import Feature

from app.features.tasks.infrastructure.provider import TaskProvider
from app.features.tasks.presentation.router import router

feature = Feature(
    name="tasks",
    routers=(router,),
    providers=(TaskProvider,),
)
