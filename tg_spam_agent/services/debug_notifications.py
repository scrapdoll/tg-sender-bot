from __future__ import annotations

import html
import logging
import time
import traceback

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tg_spam_agent.config import Settings
from tg_spam_agent.repositories import SystemRepository

logger = logging.getLogger(__name__)


class SenderDebugNotifier:
    def __init__(
        self,
        bot: Bot,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self.bot = bot
        self.session_factory = session_factory
        self.settings = settings
        self._last_sent_at: dict[str, float] = {}

    async def notify(
        self,
        title: str,
        exc: BaseException,
        *,
        context: str | None = None,
    ) -> None:
        if not self.settings.sender_debug_errors_to_chat:
            return

        fingerprint = f"{title}:{type(exc).__name__}:{context or ''}:{str(exc)}"
        now = time.monotonic()
        last_sent_at = self._last_sent_at.get(fingerprint, 0)
        if now - last_sent_at < self.settings.sender_debug_error_cooldown_seconds:
            return
        self._last_sent_at[fingerprint] = now

        async with self.session_factory() as session:
            owner_ids = await SystemRepository(session).list_platform_admin_ids()

        if not owner_ids:
            return

        traceback_text = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
        body = (
            "<b>Sender debug error</b>\n"
            f"<b>Place:</b> {html.escape(title)}\n"
            f"<b>Type:</b> <code>{html.escape(type(exc).__name__)}</code>\n"
            f"<b>Error:</b> {html.escape(str(exc))}\n"
        )
        if context:
            body += f"<b>Context:</b> {html.escape(context)}\n"
        body += f"\n<pre>{html.escape(traceback_text[-2800:])}</pre>"
        body = body[:3900]

        for owner_id in owner_ids:
            try:
                await self.bot.send_message(owner_id, body)
            except Exception as notify_exc:  # noqa: BLE001
                logger.warning(
                    "Failed to send debug error notification to owner %s: %s",
                    owner_id,
                    notify_exc,
                )
