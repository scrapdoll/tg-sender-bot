from __future__ import annotations

from sqlalchemy import text

from tg_spam_agent.config import Settings
from tg_spam_agent.db import create_session_factory, init_database


async def test_init_database_adds_topic_columns_to_existing_sqlite_table() -> None:
    settings = Settings(
        manager_bot_token="token",
        telegram_api_id=1,
        telegram_api_hash="hash",
        owner_ids=(100,),
        database_url="sqlite+aiosqlite:///:memory:",
        telethon_session_path="sender.session",
        log_level="INFO",
        scheduler_poll_seconds=15,
        default_interval_minutes=60,
        default_jitter_minutes=10,
        sender_debug_errors_to_chat=False,
        sender_debug_error_cooldown_seconds=300,
    )
    session_factory = create_session_factory(settings)

    engine = session_factory.kw["bind"]
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE subscription_targets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source VARCHAR(255) NOT NULL UNIQUE,
                    chat_id BIGINT,
                    title VARCHAR(255),
                    entity_type VARCHAR(32),
                    access_type VARCHAR(32),
                    is_joined BOOLEAN NOT NULL,
                    is_enabled BOOLEAN NOT NULL,
                    join_status VARCHAR(32),
                    last_error TEXT,
                    last_checked_at DATETIME,
                    created_at DATETIME NOT NULL
                )
                """
            )
        )

    await init_database(session_factory, settings)

    async with engine.begin() as conn:
        rows = await conn.exec_driver_sql("PRAGMA table_info(subscription_targets)")
        columns = {row[1] for row in rows}

    assert "topic_id" in columns
    assert "topic_title" in columns
