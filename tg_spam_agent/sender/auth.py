from __future__ import annotations

import getpass
import logging

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from tg_spam_agent.config import Settings

logger = logging.getLogger(__name__)


async def init_userbot_session(settings: Settings) -> None:
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        raise RuntimeError("TELEGRAM_API_ID and TELEGRAM_API_HASH are required.")

    client = TelegramClient(
        settings.session_name, settings.telegram_api_id, settings.telegram_api_hash
    )
    await client.connect()
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            logger.info("Session already authorized as %s", getattr(me, "id", "unknown"))
            return

        phone = input("Telegram phone number (international format): ").strip()
        sent = await client.send_code_request(phone)
        code = input("Login code: ").strip()

        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
        except SessionPasswordNeededError:
            password = getpass.getpass("Two-step verification password: ")
            await client.sign_in(password=password)

        me = await client.get_me()
        logger.info("Authorized successfully as %s", getattr(me, "id", "unknown"))
    finally:
        await client.disconnect()
