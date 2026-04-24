from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _parse_owner_ids(value: str) -> tuple[int, ...]:
    owner_ids: list[int] = []
    for chunk in value.split(","):
        item = chunk.strip()
        if not item:
            continue
        owner_ids.append(int(item))
    return tuple(owner_ids)


def _sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.resolve().as_posix()}"


@dataclass(slots=True)
class Settings:
    manager_bot_token: str
    telegram_api_id: int
    telegram_api_hash: str
    owner_ids: tuple[int, ...]
    database_url: str
    telethon_session_path: Path
    log_level: str
    scheduler_poll_seconds: int
    default_interval_minutes: int
    default_jitter_minutes: int

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv()

        database_path = Path(os.getenv("DATABASE_PATH", "./data/app.db"))
        telethon_session_path = Path(
            os.getenv("TELETHON_SESSION_PATH", "./data/sender_userbot.session")
        )
        database_url = os.getenv("DATABASE_URL", _sqlite_url(database_path))

        return cls(
            manager_bot_token=os.getenv("MANAGER_BOT_TOKEN", ""),
            telegram_api_id=int(os.getenv("TELEGRAM_API_ID", "0")),
            telegram_api_hash=os.getenv("TELEGRAM_API_HASH", ""),
            owner_ids=_parse_owner_ids(os.getenv("OWNER_IDS", "")),
            database_url=database_url,
            telethon_session_path=telethon_session_path,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            scheduler_poll_seconds=int(os.getenv("SCHEDULER_POLL_SECONDS", "15")),
            default_interval_minutes=int(
                os.getenv("DEFAULT_INTERVAL_MINUTES", "60")
            ),
            default_jitter_minutes=int(os.getenv("DEFAULT_JITTER_MINUTES", "10")),
        )

    def ensure_runtime_dirs(self) -> None:
        if self.database_url.startswith("sqlite+aiosqlite:///"):
            db_path = Path(self.database_url.removeprefix("sqlite+aiosqlite:///"))
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self.telethon_session_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def session_name(self) -> str:
        return str(self.telethon_session_path)
