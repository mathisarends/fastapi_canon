from abc import ABC, abstractmethod
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models import DatabaseModel


class SqlRepository[ModelT: DatabaseModel, DomainT](ABC):
    """Reusable SQL mechanics; feature repositories define meaningful operations."""

    def __init__(self, session: AsyncSession, model: type[ModelT]) -> None:
        self._session = session
        self._model = model

    @abstractmethod
    def to_domain(self, model: ModelT) -> DomainT: ...

    @abstractmethod
    def to_model(self, entity: DomainT) -> ModelT: ...

    async def find_by_id(self, entity_id: UUID) -> DomainT | None:
        model = await self._session.get(self._model, entity_id)
        return self.to_domain(model) if model is not None else None

    async def find_one_by(self, **filters: object) -> DomainT | None:
        statement = select(self._model).filter_by(**filters)
        model = await self._session.scalar(statement)
        return self.to_domain(model) if model is not None else None

    async def save_entity(self, entity: DomainT) -> DomainT:
        model = await self._session.merge(self.to_model(entity))
        await self._session.flush()
        await self._session.refresh(model)
        return self.to_domain(model)

    async def delete_entity(self, entity_id: UUID) -> bool:
        model = await self._session.get(self._model, entity_id)
        if model is None:
            return False
        await self._session.delete(model)
        await self._session.flush()
        return True
