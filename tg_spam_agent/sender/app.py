from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
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
from telethon.tl.functions.messages import ImportChatInviteRequest, SendMessageRequest

from tg_spam_agent.config import Settings
from tg_spam_agent.repositories import (
    DeliveryRepository,
    InboundRepository,
    MessageRepository,
    SubscriptionRepository,
    SystemRepository,
)
from tg_spam_agent.services.debug_notifications import SenderDebugNotifier
from tg_spam_agent.services.notifications import build_inbound_notification
from tg_spam_agent.services.scheduler import compute_next_broadcast_time, is_broadcast_due
from tg_spam_agent.services.source_parser import parse_target_source

logger = logging.getLogger(__name__)

_ALLOW_PAYMENT_REQUIRED_RE = re.compile(r"ALLOW_PAYMENT_REQUIRED_(\d+)")


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
    if isinstance(entity, types.User):
        return "user"
    return "unknown"


def _extract_id(entity: object) -> int | None:
    return getattr(entity, "id", None)


def _extract_title(entity: object) -> str | None:
    if isinstance(entity, types.Channel):
        return entity.title
    if isinstance(entity, types.Chat):
        return entity.title
    if isinstance(entity, types.User):
        full_name = " ".join(
            part for part in [entity.first_name, entity.last_name] if part
        )
        return full_name or entity.username
    return None


async def _resolve_broadcast_entity(client: TelegramClient, target) -> object:
    try:
        parsed = parse_target_source(target.source)
    except ValueError:
        parsed = None

    lookup_candidates: list[object] = []
    if parsed is not None:
        if parsed.access_type in {"public", "public_topic"}:
            lookup_candidates.append(parsed.lookup_value)
        elif parsed.access_type == "user":
            lookup_candidates.append(int(parsed.lookup_value))
        elif parsed.access_type == "private_topic":
            lookup_candidates.append(int(parsed.lookup_value))
    if target.chat_id is not None:
        lookup_candidates.append(target.chat_id)

    for candidate in lookup_candidates:
        try:
            return await client.get_entity(candidate)
        except (ValueError, TypeError, RPCError):
            continue

    # Refresh the entity cache after restarts; joined private chats may only
    # become resolvable once dialogs are loaded.
    with contextlib.suppress(Exception):
        await client.get_dialogs()
        for candidate in lookup_candidates:
            with contextlib.suppress(Exception):
                return await client.get_entity(candidate)

    if target.chat_id is None:
        raise ValueError(f"Target #{target.id} has no chat_id.")
    return target.chat_id


def _extract_required_paid_stars(exc: RPCError) -> int | None:
    error_text = " ".join(
        str(value)
        for value in [
            getattr(exc, "message", None),
            getattr(exc, "code", None),
            str(exc),
        ]
        if value is not None
    )
    match = _ALLOW_PAYMENT_REQUIRED_RE.search(error_text)
    return int(match.group(1)) if match else None


async def _send_broadcast_message(
    client: TelegramClient,
    entity: object,
    text: str,
    target,
    settings,
    *,
    allow_paid_stars: int | None = None,
) -> None:
    input_entity = await client.get_input_entity(entity)
    reply_to = (
        types.InputReplyToMessage(target.topic_id)
        if target.topic_id is not None
        else None
    )
    await client(
        SendMessageRequest(
            peer=input_entity,
            message=text,
            random_id=random.randint(-(2**63), 2**63 - 1),
            reply_to=reply_to,
            allow_paid_stars=allow_paid_stars,
        )
    )


async def _send_broadcast_with_paid_retry(
    client: TelegramClient,
    entity: object,
    text: str,
    target,
    settings,
) -> int:
    try:
        await _send_broadcast_message(client, entity, text, target, settings)
        return 0
    except RPCError as exc:
        required_stars = _extract_required_paid_stars(exc)
        if required_stars is None:
            raise
        if not settings.allow_paid_messages:
            raise
        if required_stars > settings.max_paid_message_stars:
            raise
        await _send_broadcast_message(
            client,
            entity,
            text,
            target,
            settings,
            allow_paid_stars=required_stars,
        )
        return required_stars


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


async def _mark_inbound_as_read(client: TelegramClient, event) -> None:
    try:
        await client.send_read_acknowledge(event.chat_id, max_id=event.message.id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to mark inbound message as read: %s", exc)


async def _process_inbound_event(
    client: TelegramClient,
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
        already_notified_sender = await inbound_repo.has_events_from_sender(
            event.sender_id
        )
        inbound = await inbound_repo.log_inbound_event(
            sender_id=event.sender_id,
            username=getattr(sender, "username", None),
            full_name=full_name or None,
            message_preview=preview,
            message_type=message_type,
        )
        owner_ids = (
            []
            if already_notified_sender
            else await system_repo.list_owner_ids()
        )

    await _mark_inbound_as_read(client, event)

    if owner_ids:
        await _notify_owners(bot, owner_ids, build_inbound_notification(inbound))


async def _attempt_join_target(
    client: TelegramClient,
    session_factory: async_sessionmaker[AsyncSession],
    target_id: int,
    debug_notifier: SenderDebugNotifier,
) -> None:
    async with session_factory() as session:
        repo = SubscriptionRepository(session)
        target = await repo.get_target(target_id)
        if target is None:
            return

    try:
        parsed = parse_target_source(target.source)
    except ValueError as exc:
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
        await debug_notifier.notify(
            "join target parse",
            exc,
            context=f"target_id={target_id} source={target.source}",
        )
        return

    entity = None
    try:
        if parsed.access_type in {"public", "public_topic"}:
            entity = await client.get_entity(parsed.lookup_value)
            if not isinstance(entity, types.User):
                await client(JoinChannelRequest(entity))
        elif parsed.access_type == "user":
            entity = await client.get_entity(int(parsed.lookup_value))
        elif parsed.access_type == "private_topic":
            entity = await client.get_entity(int(parsed.lookup_value))
        else:
            updates = await client(ImportChatInviteRequest(parsed.lookup_value))
            entity = updates.chats[0] if getattr(updates, "chats", None) else None
    except UserAlreadyParticipantError:
        if parsed.access_type in {"public", "public_topic"}:
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
        await debug_notifier.notify(
            "join target approval pending",
            exc,
            context=f"target_id={target_id} source={target.source}",
        )
        return
    except (ValueError, ChannelPrivateError, InviteHashExpiredError, UsernameInvalidError, UsernameNotOccupiedError) as exc:
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
        await debug_notifier.notify(
            "join target failed",
            exc,
            context=f"target_id={target_id} source={target.source}",
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
                join_status="error",
                last_error=f"Flood wait for {exc.seconds} seconds",
            )
        await debug_notifier.notify(
            "join target flood wait",
            exc,
            context=f"target_id={target_id} source={target.source}",
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
        await debug_notifier.notify(
            "join target rpc error",
            exc,
            context=f"target_id={target_id} source={target.source}",
        )
        return
    except Exception as exc:  # noqa: BLE001
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
        await debug_notifier.notify(
            "join target unexpected error",
            exc,
            context=f"target_id={target_id} source={target.source}",
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
        await SystemRepository(session).update_settings(next_broadcast_at=None)


async def _process_pending_joins(
    client: TelegramClient,
    session_factory: async_sessionmaker[AsyncSession],
    debug_notifier: SenderDebugNotifier,
) -> None:
    async with session_factory() as session:
        targets = await SubscriptionRepository(session).list_pending_targets()

    for target in targets:
        try:
            await _attempt_join_target(client, session_factory, target.id, debug_notifier)
        except Exception as exc:  # noqa: BLE001
            async with session_factory() as session:
                await SubscriptionRepository(session).mark_join_result(
                    target.id,
                    chat_id=None,
                    title=target.title,
                    entity_type=target.entity_type,
                    is_joined=False,
                    join_status="error",
                    last_error=str(exc),
                )
            logger.exception("Pending join failed for target %s: %s", target.id, exc)
            await debug_notifier.notify(
                "pending join boundary",
                exc,
                context=f"target_id={target.id} source={target.source}",
            )


async def _run_single_broadcast(
    client: TelegramClient,
    session_factory: async_sessionmaker[AsyncSession],
    debug_notifier: SenderDebugNotifier,
) -> None:
    async with session_factory() as session:
        system_repo = SystemRepository(session)
        message_repo = MessageRepository(session)
        target_repo = SubscriptionRepository(session)
        settings = await system_repo.get_settings()
        if settings is None or not settings.is_active:
            logger.debug("Broadcast skipped: settings missing or inactive")
            return

        now = datetime.now(timezone.utc)
        if not is_broadcast_due(settings.next_broadcast_at, now):
            logger.debug(
                "Broadcast skipped: next run is %s, now is %s",
                settings.next_broadcast_at,
                now,
            )
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
            logger.debug(
                "Broadcast cycle skipped: active_message=%s enabled_joined_targets=%s",
                message is not None,
                len(targets),
            )
            return

    for target in targets:
        try:
            entity = await _resolve_broadcast_entity(client, target)
            paid_stars_used = await _send_broadcast_with_paid_retry(
                client,
                entity,
                message.text,
                target,
                settings,
            )
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
            await debug_notifier.notify(
                "broadcast target disabled",
                exc,
                context=f"target_id={target.id} chat_id={target.chat_id} topic_id={target.topic_id}",
            )
        except RPCError as exc:
            async with session_factory() as session:
                await DeliveryRepository(session).log_delivery(
                    target_id=target.id,
                    message_template_id=message.id,
                    success=False,
                    error=str(exc),
                )
            await debug_notifier.notify(
                "broadcast rpc error",
                exc,
                context=f"target_id={target.id} chat_id={target.chat_id} topic_id={target.topic_id}",
            )
        except Exception as exc:  # noqa: BLE001
            async with session_factory() as session:
                await DeliveryRepository(session).log_delivery(
                    target_id=target.id,
                    message_template_id=message.id,
                    success=False,
                    error=str(exc),
                )
            logger.exception("Broadcast failed for target %s: %s", target.id, exc)
            await debug_notifier.notify(
                "broadcast unexpected error",
                exc,
                context=f"target_id={target.id} chat_id={target.chat_id} topic_id={target.topic_id}",
            )
        else:
            logger.info(
                "Broadcast delivered to target %s paid_stars=%s",
                target.id,
                paid_stars_used,
            )
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


async def _reset_empty_broadcast_schedule(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        system_repo = SystemRepository(session)
        settings = await system_repo.get_settings()
        if (
            settings is None
            or not settings.is_active
            or settings.next_broadcast_at is None
        ):
            return
        if settings.last_broadcast_at is None:
            await system_repo.update_settings(next_broadcast_at=None)
            return
        has_success = await DeliveryRepository(session).has_success_since(
            settings.last_broadcast_at
        )
        if not has_success:
            logger.info(
                "Resetting broadcast schedule because the previous due cycle had no successful deliveries"
            )
            await system_repo.update_settings(next_broadcast_at=None)


async def _sender_loop(
    client: TelegramClient,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    debug_notifier: SenderDebugNotifier,
) -> None:
    me = await client.get_me()
    self_id = me.id

    @client.on(events.NewMessage(incoming=True))
    async def on_new_message(event) -> None:
        await _process_inbound_event(client, event, bot, session_factory, self_id)

    while True:
        try:
            await _process_pending_joins(client, session_factory, debug_notifier)
            await _run_single_broadcast(client, session_factory, debug_notifier)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Sender loop iteration failed: %s", exc)
            await debug_notifier.notify("sender loop iteration", exc)
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
    await _reset_empty_broadcast_schedule(session_factory)
    debug_notifier = SenderDebugNotifier(bot, session_factory, settings)
    sender_task = asyncio.create_task(
        _sender_loop(client, bot, session_factory, settings, debug_notifier),
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
