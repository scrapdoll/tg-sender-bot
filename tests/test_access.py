from __future__ import annotations

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tg_spam_agent.models import Base
from tg_spam_agent.repositories import AccessRepository, ManagerPreferenceRepository
from tg_spam_agent.repositories import SystemRepository, TenantRepository
from tg_spam_agent.services.access import AccessService


async def test_access_service_owner_and_whitelist() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        await SystemRepository(session).ensure_defaults((900,), 60, 5)
        tenant = await TenantRepository(session).ensure_tenant_for_user(
            100,
            default_interval_minutes=60,
            default_jitter_minutes=5,
        )
        access_repo = AccessRepository(session, tenant.id)
        access_service = AccessService(access_repo)

        assert await access_service.can_manage(100) is True
        assert await access_service.can_manage(200) is False
        assert await access_service.can_admin_platform(900) is True

        await access_repo.add_whitelist_user(200)
        assert await access_service.can_manage(200) is True


async def test_manager_language_defaults_to_russian_and_can_switch() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        await SystemRepository(session).ensure_defaults((1,), 60, 5)
        tenant = await TenantRepository(session).ensure_tenant_for_user(
            100,
            default_interval_minutes=60,
            default_jitter_minutes=5,
        )
        repo = ManagerPreferenceRepository(session, tenant.id)

        assert await repo.get_language(100) == "ru"

        await repo.set_language(100, "en")
        assert await repo.get_language(100) == "en"

        await repo.set_language(100, "unknown")
        assert await repo.get_language(100) == "ru"
