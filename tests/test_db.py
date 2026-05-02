from __future__ import annotations

from sqlalchemy import select

from tg_spam_agent.config import Settings
from tg_spam_agent.db import create_session_factory, init_database
from tg_spam_agent.models import PlatformAdmin, SubscriptionPlan


async def test_init_database_seeds_platform_admins_and_default_plan() -> None:
    settings = Settings(
        manager_bot_token="token",
        telegram_api_id=1,
        telegram_api_hash="hash",
        platform_admin_ids=(100,),
        database_url="sqlite+aiosqlite:///:memory:",
        session_encryption_key="secret",
        log_level="INFO",
        scheduler_poll_seconds=15,
        default_interval_minutes=60,
        default_jitter_minutes=10,
        sender_debug_errors_to_chat=False,
        sender_debug_error_cooldown_seconds=300,
    )
    session_factory = create_session_factory(settings)

    await init_database(session_factory, settings)

    async with session_factory() as session:
        admin = await session.get(PlatformAdmin, 100)
        plan = await session.scalar(select(SubscriptionPlan))

    assert admin is not None
    assert plan is not None
    assert plan.price_stars == 500
    assert plan.max_targets == 100
    assert plan.max_templates == 10
