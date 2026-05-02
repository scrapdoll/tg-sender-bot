from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


def _parse_user_ids(value: str) -> tuple[int, ...]:
    user_ids: list[int] = []
    for chunk in value.split(","):
        item = chunk.strip()
        if not item:
            continue
        user_ids.append(int(item))
    return tuple(user_ids)


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(slots=True)
class Settings:
    manager_bot_token: str
    telegram_api_id: int
    telegram_api_hash: str
    platform_admin_ids: tuple[int, ...]
    database_url: str
    session_encryption_key: str
    log_level: str
    scheduler_poll_seconds: int
    default_interval_minutes: int
    default_jitter_minutes: int
    sender_debug_errors_to_chat: bool
    sender_debug_error_cooldown_seconds: int

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv()

        platform_admin_ids = os.getenv("PLATFORM_ADMIN_IDS")
        if platform_admin_ids is None:
            platform_admin_ids = os.getenv("OWNER_IDS", "")

        return cls(
            manager_bot_token=os.getenv("MANAGER_BOT_TOKEN", ""),
            telegram_api_id=int(os.getenv("TELEGRAM_API_ID", "0")),
            telegram_api_hash=os.getenv("TELEGRAM_API_HASH", ""),
            platform_admin_ids=_parse_user_ids(platform_admin_ids),
            database_url=os.getenv(
                "DATABASE_URL",
                "postgresql+asyncpg://postgres:postgres@localhost:5432/tg_spam_agent",
            ),
            session_encryption_key=os.getenv("SESSION_ENCRYPTION_KEY", ""),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            scheduler_poll_seconds=int(os.getenv("SCHEDULER_POLL_SECONDS", "15")),
            default_interval_minutes=int(
                os.getenv("DEFAULT_INTERVAL_MINUTES", "60")
            ),
            default_jitter_minutes=int(os.getenv("DEFAULT_JITTER_MINUTES", "10")),
            sender_debug_errors_to_chat=_parse_bool(
                os.getenv("SENDER_DEBUG_ERRORS_TO_CHAT", "false")
            ),
            sender_debug_error_cooldown_seconds=int(
                os.getenv("SENDER_DEBUG_ERROR_COOLDOWN_SECONDS", "300")
            ),
        )

    def ensure_runtime_dirs(self) -> None:
        return None

    @property
    def owner_ids(self) -> tuple[int, ...]:
        return self.platform_admin_ids
