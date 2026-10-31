from typing import Annotated
from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import Query, status
from fastapi_canon import CanonRouter

from app.authentication.presentation import AuthenticatedUserId
from app.authentication.presentation.errors import authentication_required
from app.features.tasks.application import TaskService
from app.features.tasks.presentation.errors import errors, task_not_found
from app.features.tasks.presentation.mapper import to_list_response, to_response
from app.features.tasks.presentation.schemas import (
    CreateTaskRequest,
    TaskListResponse,
    TaskResponse,
)

router = CanonRouter(
    prefix="/tasks",
    tags=["tasks"],
    route_class=DishkaRoute,
    error_registry=errors,
    raises=[authentication_required],
)


@router.post(
    "",
    operation_id="create_task",
    response_model=TaskResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_task(
    body: CreateTaskRequest,
    authenticated_user_id: AuthenticatedUserId,
    task_service: FromDishka[TaskService],
) -> TaskResponse:
    task = await task_service.create(
        user_id=authenticated_user_id,
        title=body.title,
    )
    return to_response(task)


@router.get(
    "",
    operation_id="list_tasks",
    response_model=TaskListResponse,
    status_code=status.HTTP_200_OK,
)
async def list_tasks(
    authenticated_user_id: AuthenticatedUserId,
    task_service: FromDishka[TaskService],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> TaskListResponse:
    tasks = await task_service.list_recent(
        user_id=authenticated_user_id,
        limit=limit,
    )
    return to_list_response(tasks)


@router.post(
    "/{task_id}/completion",
    operation_id="complete_task",
    response_model=TaskResponse,
    status_code=status.HTTP_200_OK,
    raises=[task_not_found],
)
async def complete_task(
    task_id: UUID,
    authenticated_user_id: AuthenticatedUserId,
    task_service: FromDishka[TaskService],
) -> TaskResponse:
    task = await task_service.complete(
        user_id=authenticated_user_id,
        task_id=task_id,
    )
    return to_response(task)
