from __future__ import annotations

from random import Random

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tg_spam_agent.models import Base
from tg_spam_agent.repositories import (
    MessageRepository,
    SubscriptionRepository,
    SystemRepository,
)


async def test_message_repository_random_only_from_enabled() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        system_repo = SystemRepository(session)
        await system_repo.ensure_defaults((1,), 60, 5)

        repo = MessageRepository(session)
        first = await repo.create_message("first", created_by=1)
        second = await repo.create_message("second", created_by=1)
        await repo.toggle_message(second.id)

        chosen = await repo.choose_random_active_message(Random(1))
        assert chosen is not None
        assert chosen.id == first.id


async def test_subscription_repository_upsert_and_retry() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        repo = SubscriptionRepository(session)
        target = await repo.upsert_target("@publictarget/77", "public_topic", 77)
        assert target.join_status == "pending"
        assert target.topic_id == 77

        updated = await repo.mark_join_result(
            target.id,
            chat_id=123,
            title="Target",
            entity_type="channel",
            is_joined=True,
            join_status="joined",
            last_error=None,
        )
        assert updated.is_joined is True

        retried = await repo.queue_retry(target.id)
        assert retried.join_status == "retry"
