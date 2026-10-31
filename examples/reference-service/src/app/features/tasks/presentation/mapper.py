from app.features.tasks.domain import Task
from app.features.tasks.presentation.schemas import TaskListResponse, TaskResponse


def to_response(task: Task) -> TaskResponse:
    return TaskResponse(
        id=task.id,
        title=task.title,
        completed=task.completed,
        created_at=task.created_at,
    )


def to_list_response(tasks: list[Task]) -> TaskListResponse:
    return TaskListResponse(items=[to_response(task) for task in tasks])
