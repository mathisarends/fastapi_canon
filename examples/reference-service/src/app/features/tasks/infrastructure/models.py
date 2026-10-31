from uuid import UUID

from sqlmodel import Field

from app.infrastructure.database import DatabaseModel


class TaskModel(DatabaseModel, table=True):
    __tablename__ = "tasks"

    user_id: UUID = Field(index=True)
    title: str = Field(min_length=1, max_length=200)
    completed: bool = False
