from __future__ import annotations

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tg_spam_agent.models import Base, Owner
from tg_spam_agent.repositories import AccessRepository, ManagerPreferenceRepository
from tg_spam_agent.services.access import AccessService


async def test_access_service_owner_and_whitelist() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(Owner(user_id=100))
        await session.commit()
        access_repo = AccessRepository(session)
        access_service = AccessService(access_repo)

        assert await access_service.can_manage(100) is True
        assert await access_service.can_manage(200) is False

        await access_repo.add_whitelist_user(200)
        assert await access_service.can_manage(200) is True


async def test_manager_language_defaults_to_russian_and_can_switch() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        repo = ManagerPreferenceRepository(session)

        assert await repo.get_language(100) == "ru"

        await repo.set_language(100, "en")
        assert await repo.get_language(100) == "en"

        await repo.set_language(100, "unknown")
        assert await repo.get_language(100) == "ru"
