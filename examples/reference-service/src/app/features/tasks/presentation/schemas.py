from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.presentation.schema import Schema


class CreateTaskRequest(Schema):
    title: str = Field(min_length=1, max_length=200)


class TaskResponse(Schema):
    id: UUID
    title: str
    completed: bool
    created_at: datetime


class TaskListResponse(Schema):
    items: list[TaskResponse]
