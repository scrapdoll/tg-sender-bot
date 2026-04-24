from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timezone
from random import Random

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telethon import TelegramClient, events, types
from telethon.errors import (
    ChannelPrivateError,
    ChatWriteForbiddenError,
    FloodWaitError,
    InviteHashExpiredError,
    InviteRequestSentError,
    RPCError,
    UserAlreadyParticipantError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

from tg_spam_agent.config import Settings
from tg_spam_agent.repositories import (
    DeliveryRepository,
    InboundRepository,
    MessageRepository,
    SubscriptionRepository,
    SystemRepository,
)
from tg_spam_agent.services.notifications import build_inbound_notification
from tg_spam_agent.services.scheduler import compute_next_broadcast_time
from tg_spam_agent.services.source_parser import parse_target_source

logger = logging.getLogger(__name__)


def _preview_text(event) -> tuple[str | None, str]:
    if event.raw_text:
        return event.raw_text[:500], "text"
    if event.message.sticker:
        return "<sticker>", "sticker"
    if event.message.photo:
        return event.message.message[:500] if event.message.message else "<photo>", "photo"
    if event.message.voice:
        return "<voice>", "voice"
    if event.message.video:
        return "<video>", "video"
    return "<unsupported>", "other"


def _entity_kind(entity: object) -> str:
    if isinstance(entity, types.Channel):
        if entity.broadcast:
            return "channel"
        if entity.megagroup:
            return "megagroup"
        return "channel"
    if isinstance(entity, types.Chat):
        return "group"
    return "unknown"


def _extract_id(entity: object) -> int | None:
    return getattr(entity, "id", None)


def _extract_title(entity: object) -> str | None:
    if isinstance(entity, types.Channel):
        return entity.title
    if isinstance(entity, types.Chat):
        return entity.title
    return None


async def _notify_owners(
    bot: Bot, owner_ids: list[int], text: str, *, disable_notification: bool = False
) -> None:
    for owner_id in owner_ids:
        try:
            await bot.send_message(
                owner_id, text, disable_notification=disable_notification
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to notify owner %s: %s", owner_id, exc)


async def _process_inbound_event(
    event,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    self_id: int,
) -> None:
    if event.out or not event.is_private or event.sender_id == self_id:
        return

    preview, message_type = _preview_text(event)
    sender = await event.get_sender()
    full_name = " ".join(
        part for part in [getattr(sender, "first_name", None), getattr(sender, "last_name", None)] if part
    )

    async with session_factory() as session:
        inbound_repo = InboundRepository(session)
        system_repo = SystemRepository(session)
        inbound = await inbound_repo.log_inbound_event(
            sender_id=event.sender_id,
            username=getattr(sender, "username", None),
            full_name=full_name or None,
            message_preview=preview,
            message_type=message_type,
        )
        owner_ids = await system_repo.list_owner_ids()

    await _notify_owners(bot, owner_ids, build_inbound_notification(inbound))


async def _attempt_join_target(
    client: TelegramClient,
    session_factory: async_sessionmaker[AsyncSession],
    target_id: int,
) -> None:
    async with session_factory() as session:
        repo = SubscriptionRepository(session)
        target = await repo.get_target(target_id)
        if target is None:
            return

    parsed = parse_target_source(target.source)
    entity = None
    try:
        if parsed.access_type == "public":
            entity = await client.get_entity(parsed.lookup_value)
            await client(JoinChannelRequest(entity))
        else:
            updates = await client(ImportChatInviteRequest(parsed.lookup_value))
            entity = updates.chats[0] if getattr(updates, "chats", None) else None
    except UserAlreadyParticipantError:
        if parsed.access_type == "public":
            entity = await client.get_entity(parsed.lookup_value)
    except InviteRequestSentError as exc:
        async with session_factory() as session:
            await SubscriptionRepository(session).mark_join_result(
                target_id,
                chat_id=None,
                title=None,
                entity_type="unknown",
                is_joined=False,
                join_status="approval_pending",
                last_error=str(exc),
            )
        return
    except (ChannelPrivateError, InviteHashExpiredError, UsernameInvalidError, UsernameNotOccupiedError) as exc:
        async with session_factory() as session:
            await SubscriptionRepository(session).mark_join_result(
                target_id,
                chat_id=None,
                title=None,
                entity_type="unknown",
                is_joined=False,
                join_status="error",
                last_error=str(exc),
            )
        return
    except FloodWaitError as exc:
        async with session_factory() as session:
            await SubscriptionRepository(session).mark_join_result(
                target_id,
                chat_id=None,
                title=None,
                entity_type="unknown",
                is_joined=False,
                join_status="retry",
                last_error=f"Flood wait for {exc.seconds} seconds",
            )
        return
    except RPCError as exc:
        async with session_factory() as session:
            await SubscriptionRepository(session).mark_join_result(
                target_id,
                chat_id=None,
                title=None,
                entity_type="unknown",
                is_joined=False,
                join_status="error",
                last_error=str(exc),
            )
        return

    async with session_factory() as session:
        await SubscriptionRepository(session).mark_join_result(
            target_id,
            chat_id=_extract_id(entity),
            title=_extract_title(entity) or target.source,
            entity_type=_entity_kind(entity),
            is_joined=True,
            join_status="joined",
            last_error=None,
        )


async def _process_pending_joins(
    client: TelegramClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        targets = await SubscriptionRepository(session).list_pending_targets()

    for target in targets:
        await _attempt_join_target(client, session_factory, target.id)


async def _run_single_broadcast(
    client: TelegramClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        system_repo = SystemRepository(session)
        message_repo = MessageRepository(session)
        target_repo = SubscriptionRepository(session)
        settings = await system_repo.get_settings()
        if settings is None or not settings.is_active:
            return

        now = datetime.now(timezone.utc)
        if settings.next_broadcast_at and settings.next_broadcast_at > now:
            return

        message = await message_repo.choose_random_active_message(Random())
        targets = await target_repo.list_enabled_joined_targets()
        next_run = compute_next_broadcast_time(
            now=now,
            base_interval_minutes=settings.base_interval_minutes,
            jitter_minutes=settings.jitter_minutes,
            rng=Random(),
        )

        if message is None or not targets:
            await system_repo.update_settings(
                last_broadcast_at=now,
                next_broadcast_at=next_run,
            )
            return

    for target in targets:
        try:
            await client.send_message(target.chat_id, message.text)
        except (ChatWriteForbiddenError, ChannelPrivateError) as exc:
            async with session_factory() as session:
                await DeliveryRepository(session).log_delivery(
                    target_id=target.id,
                    message_template_id=message.id,
                    success=False,
                    error=str(exc),
                )
                await SubscriptionRepository(session).disable_target_with_error(
                    target.id, str(exc)
                )
        except RPCError as exc:
            async with session_factory() as session:
                await DeliveryRepository(session).log_delivery(
                    target_id=target.id,
                    message_template_id=message.id,
                    success=False,
                    error=str(exc),
                )
        else:
            async with session_factory() as session:
                await DeliveryRepository(session).log_delivery(
                    target_id=target.id,
                    message_template_id=message.id,
                    success=True,
                )

    async with session_factory() as session:
        await SystemRepository(session).update_settings(
            last_broadcast_at=now,
            next_broadcast_at=next_run,
        )


async def _sender_loop(
    client: TelegramClient,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    me = await client.get_me()
    self_id = me.id

    @client.on(events.NewMessage(incoming=True))
    async def on_new_message(event) -> None:
        await _process_inbound_event(event, bot, session_factory, self_id)

    while True:
        try:
            await _process_pending_joins(client, session_factory)
            await _run_single_broadcast(client, session_factory)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Sender loop iteration failed: %s", exc)
        await asyncio.sleep(settings.scheduler_poll_seconds)


async def run_sender_userbot(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        raise RuntimeError("TELEGRAM_API_ID and TELEGRAM_API_HASH are required.")
    if not settings.manager_bot_token:
        raise RuntimeError(
            "MANAGER_BOT_TOKEN is required so the sender can notify owners."
        )

    bot = Bot(
        token=settings.manager_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    client = TelegramClient(
        settings.session_name, settings.telegram_api_id, settings.telegram_api_hash
    )
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError(
            "Telethon session is not authorized. Run `tg-spam-agent init-userbot-session` first."
        )

    logger.info("Sender userbot connected")
    sender_task = asyncio.create_task(
        _sender_loop(client, bot, session_factory, settings),
        name="sender-loop",
    )
    try:
        await client.run_until_disconnected()
    finally:
        sender_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sender_task
        await client.disconnect()
        await bot.session.close()
