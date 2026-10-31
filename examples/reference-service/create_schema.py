"""Bootstrap this disposable local example; persistent schemas use Alembic."""

import asyncio

from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel

# Import table modules before reading metadata.
import app.features.tasks.infrastructure.models  # noqa: F401
from app.settings import DatabaseSettings


async def main() -> None:
    engine = create_async_engine(DatabaseSettings().url)
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
