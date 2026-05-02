from __future__ import annotations

import logging

from tg_spam_agent.config import Settings

logger = logging.getLogger(__name__)


async def init_userbot_session(settings: Settings) -> None:
    raise RuntimeError(
        "File-based init-userbot-session is deprecated. Connect userbot sessions "
        "through the manager bot Account menu."
    )
