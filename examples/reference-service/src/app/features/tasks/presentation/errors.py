from fastapi_canon import Error, ErrorRegistry

from app.authentication.presentation.errors import authentication_required
from app.features.tasks.application import TaskNotFound

task_not_found = Error(
    TaskNotFound,
    status=404,
    code="task_not_found",
    title="Task not found",
    detail="The requested task does not exist.",
)
errors = ErrorRegistry(name="tasks", errors=[authentication_required, task_not_found])
