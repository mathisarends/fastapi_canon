from datetime import datetime
from typing import Self
from uuid import UUID

from app.domain import Aggregate


class Task(Aggregate):
    def __init__(
        self,
        title: str,
        user_id: UUID,
        completed: bool = False,
        id: UUID | None = None,
        created_time: datetime | None = None,
    ) -> None:
        super().__init__(id=id, created_time=created_time)
        self._title = title
        self._user_id = user_id
        self._completed = completed

    @property
    def title(self) -> str:
        return self._title

    @property
    def user_id(self) -> UUID:
        return self._user_id

    @property
    def completed(self) -> bool:
        return self._completed

    def complete(self) -> Self:
        self._completed = True
        return self
