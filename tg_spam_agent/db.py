from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tg_spam_agent.config import Settings
from tg_spam_agent.models import Base
from tg_spam_agent.repositories import SystemRepository


def create_session_factory(settings: Settings) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(settings.database_url, future=True)
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_database(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    engine = session_factory.kw["bind"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if settings.database_url.startswith("sqlite+aiosqlite:///"):
            rows = await conn.exec_driver_sql("PRAGMA table_info(subscription_targets)")
            columns = {row[1] for row in rows}
            if "topic_id" not in columns:
                await conn.exec_driver_sql(
                    "ALTER TABLE subscription_targets ADD COLUMN topic_id INTEGER",
                )
            if "topic_title" not in columns:
                await conn.exec_driver_sql(
                    "ALTER TABLE subscription_targets ADD COLUMN topic_title VARCHAR(255)",
                )

    async with session_factory() as session:
        system_repo = SystemRepository(session)
        await system_repo.ensure_defaults(
            owner_ids=settings.owner_ids,
            default_interval_minutes=settings.default_interval_minutes,
            default_jitter_minutes=settings.default_jitter_minutes,
        )
