from __future__ import annotations

import html
import logging
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tg_spam_agent.config import Settings
from tg_spam_agent.manager.i18n import Translator
from tg_spam_agent.manager.keyboards import (
    build_inbound_users_keyboard,
    build_language_keyboard,
    build_main_keyboard,
    build_messages_keyboard,
    build_schedule_keyboard,
    build_subscription_detail_keyboard,
    build_subscriptions_keyboard,
    build_whitelist_keyboard,
)
from tg_spam_agent.repositories import (
    AccessRepository,
    InboundRepository,
    ManagerPreferenceRepository,
    MessageRepository,
    StatusRepository,
    SubscriptionRepository,
    SystemRepository,
)
from tg_spam_agent.services.access import AccessService
from tg_spam_agent.services.datetime_utils import ensure_utc
from tg_spam_agent.services.scheduler import compute_next_broadcast_time
from tg_spam_agent.services.source_parser import parse_target_source, split_target_sources

logger = logging.getLogger(__name__)


class ManagerStates(StatesGroup):
    waiting_subscription_source = State()
    waiting_message_text = State()
    waiting_interval = State()
    waiting_jitter = State()
    waiting_whitelist_add = State()
    waiting_whitelist_remove = State()


def _dt(value: datetime | None, tr: Translator) -> str:
    normalized = ensure_utc(value)
    if normalized is None:
        return tr.t("not_scheduled")
    return normalized.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _subscription_line(target, tr: Translator) -> str:
    title = html.escape(target.title or target.source)
    topic = (
        f"\n   topic_id: <code>{target.topic_id}</code>"
        if target.topic_id is not None
        else ""
    )
    error = (
        f"\n   {tr.t('target_error')}: {html.escape(target.last_error)}"
        if target.last_error
        else ""
    )
    return (
        f"#{target.id} {title}\n"
        f"   {tr.t('target_source')}: <code>{html.escape(target.source)}</code>\n"
        f"   {tr.t('target_status')}: {target.join_status} | joined={target.is_joined} | enabled={target.is_enabled}"
        f"{topic}"
        f"{error}"
    )


def _message_line(message, tr: Translator) -> str:
    preview = html.escape(message.text[:120].replace("\n", " "))
    return f"#{message.id} {tr.t('message_enabled')}={message.is_enabled} - {preview}"


def _failure_line(failure, tr: Translator) -> str:
    when = _dt(failure.attempted_at, tr)
    error = html.escape(failure.error or "unknown error")
    return f"target #{failure.target_id} at {when}: {error}"


def _inbound_sender_line(summary, tr: Translator) -> str:
    event = summary.event
    display_name = event.full_name or (
        f"@{event.username}" if event.username else tr.t("inbound_unknown_user")
    )
    username = f"@{event.username}" if event.username else "-"
    preview = html.escape((event.message_preview or "").replace("\n", " ")[:160])
    if not preview:
        preview = "-"
    return (
        f"<b>{html.escape(display_name)}</b>\n"
        f"   ID: <code>{event.sender_id}</code> | username: {html.escape(username)}\n"
        f"   {tr.t('inbound_messages_count')}: {summary.message_count} | "
        f"{tr.t('inbound_last_seen')}: {_dt(event.received_at, tr)}\n"
        f"   {tr.t('inbound_last_preview')}: {preview}"
    )


def _session_status(settings: Settings, tr: Translator) -> str:
    return tr.t("session_detected") if settings.telethon_session_path.exists() else tr.t("session_missing")


async def _safe_edit(callback: CallbackQuery, text: str, reply_markup) -> None:
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def _get_translator(
    session_factory: async_sessionmaker[AsyncSession], user_id: int | None
) -> Translator:
    if user_id is None:
        return Translator(None)
    async with session_factory() as session:
        language = await ManagerPreferenceRepository(session).get_language(user_id)
    return Translator(language)


async def _is_allowed(
    session_factory: async_sessionmaker[AsyncSession], user_id: int
) -> bool:
    async with session_factory() as session:
        access = AccessService(AccessRepository(session))
        return await access.can_manage(user_id)


async def _ensure_message_access(
    message: Message, session_factory: async_sessionmaker[AsyncSession]
) -> bool:
    if message.chat.type != ChatType.PRIVATE:
        return False
    tr = await _get_translator(session_factory, message.from_user.id)
    if not await _is_allowed(session_factory, message.from_user.id):
        await message.answer(tr.t("access_denied"))
        return False
    return True


async def _ensure_callback_access(
    callback: CallbackQuery, session_factory: async_sessionmaker[AsyncSession]
) -> bool:
    user_id = callback.from_user.id if callback.from_user else None
    tr = await _get_translator(session_factory, user_id)
    if user_id is None or not await _is_allowed(session_factory, user_id):
        await callback.answer(tr.t("access_denied"), show_alert=True)
        return False
    return True


async def _show_main(target: Message | CallbackQuery, tr: Translator) -> None:
    text = f"{tr.t('main_title')}\n{tr.t('main_description')}"
    markup = build_main_keyboard(tr)
    if isinstance(target, CallbackQuery):
        await _safe_edit(target, text, markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


async def _show_subscriptions(
    target: Message | CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    tr: Translator,
) -> None:
    async with session_factory() as session:
        repo = SubscriptionRepository(session)
        targets = await repo.list_targets()

    if targets:
        text = (
            f"{tr.t('targets_title')}\n"
            f"{tr.t('targets_hint')}\n\n"
            f"{tr.t('targets_list_hint', count=len(targets))}"
        )
    else:
        text = f"{tr.t('targets_title')}\n{tr.t('targets_hint')}\n\n{tr.t('targets_empty')}"
    markup = build_subscriptions_keyboard(targets, tr)
    if isinstance(target, CallbackQuery):
        await _safe_edit(target, text, markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


async def _show_subscription_detail(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    tr: Translator,
    target_id: int,
) -> None:
    async with session_factory() as session:
        target = await SubscriptionRepository(session).get_target(target_id)

    if target is None:
        await callback.answer(tr.t("target_not_found"), show_alert=True)
        await _show_subscriptions(callback, session_factory, tr)
        return

    text = f"{tr.t('target_details_title')}\n\n{_subscription_line(target, tr)}"
    await _safe_edit(callback, text, build_subscription_detail_keyboard(target, tr))
    await callback.answer()


async def _show_messages(
    target: Message | CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    tr: Translator,
) -> None:
    async with session_factory() as session:
        repo = MessageRepository(session)
        messages = await repo.list_messages()

    lines = [_message_line(item, tr) for item in messages] or [tr.t("messages_empty")]
    text = f"{tr.t('messages_title')}\n{tr.t('messages_hint')}\n\n" + "\n".join(lines)
    markup = build_messages_keyboard(messages, tr)
    if isinstance(target, CallbackQuery):
        await _safe_edit(target, text, markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


async def _show_schedule(
    target: Message | CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    tr: Translator,
) -> None:
    async with session_factory() as session:
        settings = await SystemRepository(session).get_settings()

    text = (
        f"{tr.t('schedule_title')}\n"
        f"{tr.t('schedule_active')}: {settings.is_active}\n"
        f"{tr.t('schedule_interval')}: {settings.base_interval_minutes} min\n"
        f"{tr.t('schedule_jitter')}: {settings.jitter_minutes} min\n"
        f"{tr.t('schedule_last_run')}: {_dt(settings.last_broadcast_at, tr)}\n"
        f"{tr.t('schedule_next_run')}: {_dt(settings.next_broadcast_at, tr)}"
    )
    markup = build_schedule_keyboard(settings, tr)
    if isinstance(target, CallbackQuery):
        await _safe_edit(target, text, markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


async def _show_whitelist(
    target: Message | CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    tr: Translator,
) -> None:
    async with session_factory() as session:
        repo = AccessRepository(session)
        entries = await repo.list_whitelist()

    lines = [f"<code>{item.user_id}</code>" for item in entries] or [tr.t("whitelist_empty")]
    text = "<b>Whitelist</b>\n\n" + "\n".join(lines)
    markup = build_whitelist_keyboard(tr)
    if isinstance(target, CallbackQuery):
        await _safe_edit(target, text, markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


async def _show_inbound_users(
    target: Message | CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    tr: Translator,
) -> None:
    async with session_factory() as session:
        summaries = await InboundRepository(session).list_sender_summaries()

    lines = (
        [_inbound_sender_line(summary, tr) for summary in summaries]
        if summaries
        else [tr.t("inbound_users_empty")]
    )
    text = (
        f"{tr.t('inbound_users_title')}\n"
        f"{tr.t('inbound_users_hint')}\n\n"
        + "\n\n".join(lines)
    )
    markup = build_inbound_users_keyboard(summaries, tr)
    if isinstance(target, CallbackQuery):
        await _safe_edit(target, text, markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


async def _show_status(
    target: Message | CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    tr: Translator,
) -> None:
    async with session_factory() as session:
        snapshot = await StatusRepository(session).get_snapshot()

    failure_lines = (
        "\n".join(_failure_line(item, tr) for item in snapshot.recent_failures)
        if snapshot.recent_failures
        else tr.t("status_no_failures")
    )
    text = (
        f"{tr.t('status_title')}\n"
        f"{tr.t('status_owners')}: {', '.join(str(owner_id) for owner_id in snapshot.owner_ids) or 'none'}\n"
        f"{tr.t('status_session')}: {_session_status(settings, tr)}\n"
        f"{tr.t('status_total_targets')}: {snapshot.counts['total_targets']}\n"
        f"{tr.t('status_joined_targets')}: {snapshot.counts['joined_targets']}\n"
        f"{tr.t('status_pending_joins')}: {snapshot.counts['pending_targets']}\n"
        f"{tr.t('status_active_messages')}: {snapshot.counts['active_messages']}\n"
        f"{tr.t('status_whitelist_size')}: {snapshot.counts['whitelist_users']}\n"
        f"{tr.t('status_broadcast_active')}: {snapshot.settings.is_active}\n"
        f"{tr.t('schedule_last_run')}: {_dt(snapshot.settings.last_broadcast_at, tr)}\n"
        f"{tr.t('schedule_next_run')}: {_dt(snapshot.settings.next_broadcast_at, tr)}\n\n"
        f"{tr.t('status_recent_failures')}\n{failure_lines}"
    )
    markup = build_main_keyboard(tr)
    if isinstance(target, CallbackQuery):
        await _safe_edit(target, text, markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


async def _show_language(
    target: CallbackQuery,
    tr: Translator,
    answer_text: str | None = None,
) -> None:
    text = f"{tr.t('language_title')}\n{tr.t('language_current')}\n{tr.t('language_choose')}"
    await _safe_edit(target, text, build_language_keyboard(tr))
    await target.answer(answer_text)


def create_manager_router(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> Router:
    router = Router()

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext) -> None:
        if not await _ensure_message_access(message, session_factory):
            return
        await state.clear()
        tr = await _get_translator(session_factory, message.from_user.id)
        await _show_main(message, tr)

    @router.message(Command("help"))
    async def help_command(message: Message, state: FSMContext) -> None:
        if not await _ensure_message_access(message, session_factory):
            return
        await state.clear()
        tr = await _get_translator(session_factory, message.from_user.id)
        await _show_main(message, tr)

    @router.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext) -> None:
        if not await _ensure_message_access(message, session_factory):
            return
        await state.clear()
        tr = await _get_translator(session_factory, message.from_user.id)
        await message.answer(tr.t("action_canceled"), reply_markup=build_main_keyboard(tr))

    @router.callback_query(F.data == "noop")
    async def noop(callback: CallbackQuery) -> None:
        await callback.answer()

    @router.callback_query(F.data == "menu:main")
    async def menu_main(callback: CallbackQuery, state: FSMContext) -> None:
        if not await _ensure_callback_access(callback, session_factory):
            return
        await state.clear()
        tr = await _get_translator(session_factory, callback.from_user.id)
        await _show_main(callback, tr)

    @router.callback_query(F.data == "menu:subscriptions")
    async def menu_subscriptions(callback: CallbackQuery, state: FSMContext) -> None:
        if not await _ensure_callback_access(callback, session_factory):
            return
        await state.clear()
        tr = await _get_translator(session_factory, callback.from_user.id)
        await _show_subscriptions(callback, session_factory, tr)

    @router.callback_query(F.data == "menu:messages")
    async def menu_messages(callback: CallbackQuery, state: FSMContext) -> None:
        if not await _ensure_callback_access(callback, session_factory):
            return
        await state.clear()
        tr = await _get_translator(session_factory, callback.from_user.id)
        await _show_messages(callback, session_factory, tr)

    @router.callback_query(F.data == "menu:schedule")
    async def menu_schedule(callback: CallbackQuery, state: FSMContext) -> None:
        if not await _ensure_callback_access(callback, session_factory):
            return
        await state.clear()
        tr = await _get_translator(session_factory, callback.from_user.id)
        await _show_schedule(callback, session_factory, tr)

    @router.callback_query(F.data == "menu:whitelist")
    async def menu_whitelist(callback: CallbackQuery, state: FSMContext) -> None:
        if not await _ensure_callback_access(callback, session_factory):
            return
        await state.clear()
        tr = await _get_translator(session_factory, callback.from_user.id)
        await _show_whitelist(callback, session_factory, tr)

    @router.callback_query(F.data == "menu:inbound_users")
    async def menu_inbound_users(callback: CallbackQuery, state: FSMContext) -> None:
        if not await _ensure_callback_access(callback, session_factory):
            return
        await state.clear()
        tr = await _get_translator(session_factory, callback.from_user.id)
        await _show_inbound_users(callback, session_factory, tr)

    @router.callback_query(F.data.startswith("inbound_user_no_link:"))
    async def inbound_user_no_link(callback: CallbackQuery) -> None:
        if not await _ensure_callback_access(callback, session_factory):
            return
        tr = await _get_translator(session_factory, callback.from_user.id)
        sender_id = callback.data.split(":", 1)[1]
        await callback.answer(
            tr.t("inbound_user_no_link", user_id=sender_id),
            show_alert=True,
        )

    @router.callback_query(F.data == "menu:status")
    async def menu_status(callback: CallbackQuery, state: FSMContext) -> None:
        if not await _ensure_callback_access(callback, session_factory):
            return
        await state.clear()
        tr = await _get_translator(session_factory, callback.from_user.id)
        await _show_status(callback, session_factory, settings, tr)

    @router.callback_query(F.data == "menu:language")
    async def menu_language(callback: CallbackQuery, state: FSMContext) -> None:
        if not await _ensure_callback_access(callback, session_factory):
            return
        await state.clear()
        tr = await _get_translator(session_factory, callback.from_user.id)
        await _show_language(callback, tr)

    @router.callback_query(F.data.startswith("lang:set:"))
    async def set_language(callback: CallbackQuery) -> None:
        if not await _ensure_callback_access(callback, session_factory):
            return
        language_code = callback.data.rsplit(":", 1)[1]
        async with session_factory() as session:
            await ManagerPreferenceRepository(session).set_language(
                callback.from_user.id,
                language_code,
            )
        tr = await _get_translator(session_factory, callback.from_user.id)
        await _show_language(callback, tr, tr.t("language_updated"))

    @router.callback_query(F.data == "sub:add")
    async def add_subscription(callback: CallbackQuery, state: FSMContext) -> None:
        if not await _ensure_callback_access(callback, session_factory):
            return
        tr = await _get_translator(session_factory, callback.from_user.id)
        await state.set_state(ManagerStates.waiting_subscription_source)
        await callback.message.answer(tr.t("send_target"))
        await callback.answer()

    @router.callback_query(F.data.startswith("sub_view:"))
    async def view_subscription(callback: CallbackQuery, state: FSMContext) -> None:
        if not await _ensure_callback_access(callback, session_factory):
            return
        await state.clear()
        tr = await _get_translator(session_factory, callback.from_user.id)
        target_id = int(callback.data.split(":")[1])
        await _show_subscription_detail(callback, session_factory, tr, target_id)

    @router.message(ManagerStates.waiting_subscription_source, F.text)
    async def subscription_source(message: Message, state: FSMContext) -> None:
        if not await _ensure_message_access(message, session_factory):
            return
        tr = await _get_translator(session_factory, message.from_user.id)
        sources = split_target_sources(message.text)
        if not sources:
            await message.answer(tr.t("invalid_target", error="empty input"))
            return

        created_targets = []
        failed_sources: list[tuple[str, str]] = []
        async with session_factory() as session:
            repo = SubscriptionRepository(session)
            for source in sources:
                try:
                    parsed = parse_target_source(source)
                except ValueError as exc:
                    failed_sources.append((source, str(exc)))
                    continue
                created_targets.append(
                    await repo.upsert_target(
                        parsed.normalized,
                        parsed.access_type,
                        parsed.topic_id,
                    )
                )

        if not created_targets:
            await message.answer(
                "\n".join(
                    tr.t("invalid_target", error=f"{source}: {error}")
                    for source, error in failed_sources
                )
            )
            return

        await state.clear()
        queued_lines = [
            tr.t(
                "target_queued_line",
                id=target.id,
                source=html.escape(target.source),
            )
            for target in created_targets
        ]
        response = [tr.t("targets_queued_summary", count=len(created_targets))]
        if failed_sources:
            response.extend(
                tr.t(
                    "target_failed_line",
                    source=html.escape(source),
                    error=html.escape(error),
                )
                for source, error in failed_sources
            )
        response.extend(queued_lines)
        await message.answer("\n".join(response))
        await _show_subscriptions(message, session_factory, tr)

    @router.callback_query(F.data.startswith("sub_toggle:"))
    async def toggle_subscription(callback: CallbackQuery) -> None:
        if not await _ensure_callback_access(callback, session_factory):
            return
        tr = await _get_translator(session_factory, callback.from_user.id)
        target_id = int(callback.data.split(":")[1])
        async with session_factory() as session:
            repo = SubscriptionRepository(session)
            target = await repo.toggle_enabled(target_id)
            if target is not None and target.is_enabled and target.is_joined:
                await SystemRepository(session).update_settings(next_broadcast_at=None)
        await _show_subscription_detail(callback, session_factory, tr, target_id)

    @router.callback_query(F.data.startswith("sub_retry:"))
    async def retry_subscription(callback: CallbackQuery) -> None:
        if not await _ensure_callback_access(callback, session_factory):
            return
        tr = await _get_translator(session_factory, callback.from_user.id)
        target_id = int(callback.data.split(":")[1])
        async with session_factory() as session:
            repo = SubscriptionRepository(session)
            await repo.queue_retry(target_id)
        await _show_subscription_detail(callback, session_factory, tr, target_id)

    @router.callback_query(F.data.startswith("sub_delete:"))
    async def delete_subscription(callback: CallbackQuery) -> None:
        if not await _ensure_callback_access(callback, session_factory):
            return
        tr = await _get_translator(session_factory, callback.from_user.id)
        target_id = int(callback.data.split(":")[1])
        async with session_factory() as session:
            repo = SubscriptionRepository(session)
            await repo.delete_target(target_id)
        await _show_subscriptions(callback, session_factory, tr)

    @router.callback_query(F.data == "msg:add")
    async def add_message(callback: CallbackQuery, state: FSMContext) -> None:
        if not await _ensure_callback_access(callback, session_factory):
            return
        tr = await _get_translator(session_factory, callback.from_user.id)
        await state.set_state(ManagerStates.waiting_message_text)
        await callback.message.answer(tr.t("send_message_text"))
        await callback.answer()

    @router.message(ManagerStates.waiting_message_text, F.text)
    async def create_message(message: Message, state: FSMContext) -> None:
        if not await _ensure_message_access(message, session_factory):
            return
        tr = await _get_translator(session_factory, message.from_user.id)
        async with session_factory() as session:
            repo = MessageRepository(session)
            created = await repo.create_message(message.text, message.from_user.id)
            await SystemRepository(session).update_settings(next_broadcast_at=None)
        await state.clear()
        await message.answer(tr.t("message_saved", id=created.id))
        await _show_messages(message, session_factory, tr)

    @router.callback_query(F.data.startswith("msg_toggle:"))
    async def toggle_message(callback: CallbackQuery) -> None:
        if not await _ensure_callback_access(callback, session_factory):
            return
        tr = await _get_translator(session_factory, callback.from_user.id)
        message_id = int(callback.data.split(":")[1])
        async with session_factory() as session:
            message = await MessageRepository(session).toggle_message(message_id)
            if message is not None and message.is_enabled:
                await SystemRepository(session).update_settings(next_broadcast_at=None)
        await _show_messages(callback, session_factory, tr)

    @router.callback_query(F.data.startswith("msg_delete:"))
    async def delete_message(callback: CallbackQuery) -> None:
        if not await _ensure_callback_access(callback, session_factory):
            return
        tr = await _get_translator(session_factory, callback.from_user.id)
        message_id = int(callback.data.split(":")[1])
        async with session_factory() as session:
            await MessageRepository(session).delete_message(message_id)
        await _show_messages(callback, session_factory, tr)

    @router.callback_query(F.data == "schedule:set_interval")
    async def schedule_set_interval(callback: CallbackQuery, state: FSMContext) -> None:
        if not await _ensure_callback_access(callback, session_factory):
            return
        tr = await _get_translator(session_factory, callback.from_user.id)
        await state.set_state(ManagerStates.waiting_interval)
        await callback.message.answer(tr.t("send_interval"))
        await callback.answer()

    @router.message(ManagerStates.waiting_interval, F.text)
    async def schedule_interval(message: Message, state: FSMContext) -> None:
        if not await _ensure_message_access(message, session_factory):
            return
        tr = await _get_translator(session_factory, message.from_user.id)
        try:
            value = max(1, int(message.text.strip()))
        except ValueError:
            await message.answer(tr.t("invalid_interval"))
            return
        async with session_factory() as session:
            repo = SystemRepository(session)
            current = await repo.get_settings()
            jitter = current.jitter_minutes if current else settings.default_jitter_minutes
            next_run = compute_next_broadcast_time(
                now=datetime.now(timezone.utc),
                base_interval_minutes=value,
                jitter_minutes=jitter,
            )
            await repo.update_settings(
                base_interval_minutes=value,
                next_broadcast_at=next_run,
            )
        await state.clear()
        await _show_schedule(message, session_factory, tr)

    @router.callback_query(F.data == "schedule:set_jitter")
    async def schedule_set_jitter(callback: CallbackQuery, state: FSMContext) -> None:
        if not await _ensure_callback_access(callback, session_factory):
            return
        tr = await _get_translator(session_factory, callback.from_user.id)
        await state.set_state(ManagerStates.waiting_jitter)
        await callback.message.answer(tr.t("send_jitter"))
        await callback.answer()

    @router.message(ManagerStates.waiting_jitter, F.text)
    async def schedule_jitter(message: Message, state: FSMContext) -> None:
        if not await _ensure_message_access(message, session_factory):
            return
        tr = await _get_translator(session_factory, message.from_user.id)
        try:
            value = max(0, int(message.text.strip()))
        except ValueError:
            await message.answer(tr.t("invalid_jitter"))
            return
        async with session_factory() as session:
            repo = SystemRepository(session)
            current = await repo.get_settings()
            interval = (
                current.base_interval_minutes
                if current
                else settings.default_interval_minutes
            )
            next_run = compute_next_broadcast_time(
                now=datetime.now(timezone.utc),
                base_interval_minutes=interval,
                jitter_minutes=value,
            )
            await repo.update_settings(
                jitter_minutes=value,
                next_broadcast_at=next_run,
            )
        await state.clear()
        await _show_schedule(message, session_factory, tr)

    @router.callback_query(F.data == "schedule:toggle")
    async def schedule_toggle(callback: CallbackQuery) -> None:
        if not await _ensure_callback_access(callback, session_factory):
            return
        tr = await _get_translator(session_factory, callback.from_user.id)
        async with session_factory() as session:
            repo = SystemRepository(session)
            current = await repo.get_settings()
            will_activate = not current.is_active
            await repo.update_settings(
                is_active=will_activate,
                next_broadcast_at=None if will_activate else current.next_broadcast_at,
            )
        await _show_schedule(callback, session_factory, tr)

    @router.callback_query(F.data == "wl:add")
    async def whitelist_add(callback: CallbackQuery, state: FSMContext) -> None:
        if not await _ensure_callback_access(callback, session_factory):
            return
        tr = await _get_translator(session_factory, callback.from_user.id)
        await state.set_state(ManagerStates.waiting_whitelist_add)
        await callback.message.answer(tr.t("send_whitelist_add"))
        await callback.answer()

    @router.message(ManagerStates.waiting_whitelist_add, F.text)
    async def whitelist_add_value(message: Message, state: FSMContext) -> None:
        if not await _ensure_message_access(message, session_factory):
            return
        tr = await _get_translator(session_factory, message.from_user.id)
        try:
            user_id = int(message.text.strip())
        except ValueError:
            await message.answer(tr.t("invalid_user_id"))
            return
        async with session_factory() as session:
            await AccessRepository(session).add_whitelist_user(user_id)
        await state.clear()
        await _show_whitelist(message, session_factory, tr)

    @router.callback_query(F.data == "wl:remove")
    async def whitelist_remove(callback: CallbackQuery, state: FSMContext) -> None:
        if not await _ensure_callback_access(callback, session_factory):
            return
        tr = await _get_translator(session_factory, callback.from_user.id)
        await state.set_state(ManagerStates.waiting_whitelist_remove)
        await callback.message.answer(tr.t("send_whitelist_remove"))
        await callback.answer()

    @router.message(ManagerStates.waiting_whitelist_remove, F.text)
    async def whitelist_remove_value(message: Message, state: FSMContext) -> None:
        if not await _ensure_message_access(message, session_factory):
            return
        tr = await _get_translator(session_factory, message.from_user.id)
        try:
            user_id = int(message.text.strip())
        except ValueError:
            await message.answer(tr.t("invalid_user_id"))
            return
        async with session_factory() as session:
            await AccessRepository(session).remove_whitelist_user(user_id)
        await state.clear()
        await _show_whitelist(message, session_factory, tr)

    @router.message(F.text)
    async def fallback(message: Message) -> None:
        if not await _ensure_message_access(message, session_factory):
            return
        tr = await _get_translator(session_factory, message.from_user.id)
        await message.answer(tr.t("fallback"))

    return router


async def run_manager_bot(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    if not settings.manager_bot_token:
        raise RuntimeError("MANAGER_BOT_TOKEN is required to run the manager bot.")

    bot = Bot(
        token=settings.manager_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(create_manager_router(session_factory, settings))
    logger.info("Starting manager bot polling")
    await dp.start_polling(bot)
