from collections.abc import AsyncIterator

from dishka import Provider, Scope, provide
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.settings import DatabaseSettings


class DatabaseProvider(Provider):
    @provide(scope=Scope.APP)
    def settings(self) -> DatabaseSettings:
        return DatabaseSettings()

    @provide(scope=Scope.APP)
    async def engine(self, settings: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
        engine = create_async_engine(settings.url, pool_pre_ping=True)
        try:
            yield engine
        finally:
            await engine.dispose()

    @provide(scope=Scope.APP)
    def session_factory(self, engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
        return async_sessionmaker(engine, expire_on_commit=False)

    @provide(scope=Scope.REQUEST)
    async def session(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
