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
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

from tg_spam_agent.config import Settings
from tg_spam_agent.manager.i18n import Translator
from tg_spam_agent.manager.keyboards import (
    build_account_keyboard,
    build_admin_keyboard,
    build_billing_keyboard,
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
    BillingRepository,
    InboundRepository,
    ManagerPreferenceRepository,
    MessageRepository,
    PlanRepository,
    StatusRepository,
    SubscriptionRepository,
    SystemRepository,
    TelegramSessionRepository,
    TenantContext,
    TenantRepository,
)
from tg_spam_agent.services.crypto import SessionCipher
from tg_spam_agent.services.datetime_utils import ensure_utc
from tg_spam_agent.services.scheduler import compute_next_broadcast_time
from tg_spam_agent.services.source_parser import parse_target_source, split_target_sources

logger = logging.getLogger(__name__)


class ManagerStates(StatesGroup):
    waiting_subscription_source = State()
    waiting_message_text = State()
    waiting_interval = State()
    waiting_jitter = State()
    waiting_paid_stars = State()
    waiting_whitelist_add = State()
    waiting_whitelist_remove = State()
    waiting_userbot_phone = State()
    waiting_userbot_code = State()
    waiting_userbot_password = State()
    waiting_admin_price = State()
    waiting_admin_max_targets = State()
    waiting_admin_max_templates = State()
    waiting_admin_min_interval = State()


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


async def _safe_edit(callback: CallbackQuery, text: str, reply_markup) -> None:
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def _get_context(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    user_id: int,
) -> TenantContext:
    async with session_factory() as session:
        return await TenantRepository(session).get_context(
            user_id,
            default_interval_minutes=settings.default_interval_minutes,
            default_jitter_minutes=settings.default_jitter_minutes,
        )


async def _get_translator(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    user_id: int | None,
) -> Translator:
    if user_id is None:
        return Translator(None)
    context = await _get_context(session_factory, settings, user_id)
    async with session_factory() as session:
        language = await ManagerPreferenceRepository(
            session, context.tenant_id
        ).get_language(user_id)
    return Translator(language)


async def _ensure_message_access(
    message: Message,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> TenantContext | None:
    if message.chat.type != ChatType.PRIVATE or message.from_user is None:
        return None
    return await _get_context(session_factory, settings, message.from_user.id)


async def _ensure_callback_access(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> TenantContext | None:
    user_id = callback.from_user.id if callback.from_user else None
    if user_id is None:
        return None
    return await _get_context(session_factory, settings, user_id)


async def _require_mutation(
    target: Message | CallbackQuery,
    context: TenantContext,
    tr: Translator,
) -> bool:
    if context.can_mutate:
        return True
    text = "Subscription is inactive. Settings are read-only until payment is active."
    if isinstance(target, CallbackQuery):
        await target.answer(text, show_alert=True)
    else:
        await target.answer(text)
    return False


async def _show_main(target: Message | CallbackQuery, tr: Translator, context: TenantContext) -> None:
    status = "active" if context.subscription_active else context.subscription_status
    text = (
        f"{tr.t('main_title')}\n{tr.t('main_description')}\n\n"
        f"Tenant: <code>{context.tenant_id}</code>\n"
        f"Subscription: <b>{html.escape(status)}</b>"
    )
    markup = build_main_keyboard(
        tr,
        show_tenant_admin=context.can_manage,
        show_platform_admin=context.is_platform_admin,
    )
    if isinstance(target, CallbackQuery):
        await _safe_edit(target, text, markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


async def _show_account(
    target: Message | CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    tr: Translator,
    context: TenantContext,
) -> None:
    async with session_factory() as session:
        userbot = await TelegramSessionRepository(session, context.tenant_id).get()
    text = (
        "<b>Account</b>\n\n"
        f"Userbot status: <b>{html.escape(userbot.status)}</b>\n"
        f"Telegram user id: <code>{userbot.telegram_user_id or '-'}</code>\n"
    )
    if userbot.last_error:
        text += f"Last error: {html.escape(userbot.last_error)}\n"
    text += "\nPhone, login code, and 2FA password are not stored."
    markup = build_account_keyboard(tr)
    if isinstance(target, CallbackQuery):
        await _safe_edit(target, text, markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


async def _show_billing(
    target: Message | CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    tr: Translator,
    context: TenantContext,
) -> None:
    async with session_factory() as session:
        plan = await PlanRepository(session).get_active_plan()
        subscription = await BillingRepository(session).get_subscription(context.tenant_id)
    period_end = _dt(subscription.current_period_end, tr) if subscription else "-"
    status = subscription.status if subscription else "inactive"
    text = (
        "<b>Subscription</b>\n\n"
        f"Status: <b>{html.escape(status)}</b>\n"
        f"Active until: {period_end}\n"
    )
    if plan is None:
        text += "\nNo active plan is available."
    else:
        text += (
            f"\nPlan: {html.escape(plan.name)}\n"
            f"Price: <b>{plan.price_stars}</b> Stars / 30 days\n"
            f"Limits: {plan.max_targets} targets, {plan.max_templates} templates, "
            f"min interval {plan.min_interval_minutes} min"
        )
    markup = build_billing_keyboard(plan, tr)
    if isinstance(target, CallbackQuery):
        await _safe_edit(target, text, markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


async def _show_admin(
    target: Message | CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    tr: Translator,
    context: TenantContext,
) -> None:
    if not context.is_platform_admin:
        if isinstance(target, CallbackQuery):
            await target.answer(tr.t("access_denied"), show_alert=True)
        return
    async with session_factory() as session:
        plan = await PlanRepository(session).get_active_plan()
        if plan is None:
            plan = await TenantRepository(session).ensure_default_plan()
    text = (
        "<b>Platform admin</b>\n\n"
        f"Plan: {html.escape(plan.name)}\n"
        f"Active: {plan.is_active}\n"
        f"Price: {plan.price_stars} Stars\n"
        f"Max targets: {plan.max_targets}\n"
        f"Max templates: {plan.max_templates}\n"
        f"Min interval: {plan.min_interval_minutes} min"
    )
    markup = build_admin_keyboard(plan, tr)
    if isinstance(target, CallbackQuery):
        await _safe_edit(target, text, markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


async def _show_subscriptions(
    target: Message | CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    tr: Translator,
    context: TenantContext,
) -> None:
    async with session_factory() as session:
        targets = await SubscriptionRepository(session, context.tenant_id).list_targets()
        plan = await PlanRepository(session).get_active_plan()

    text = f"{tr.t('targets_title')}\n{tr.t('targets_hint')}\n\n"
    if targets:
        text += tr.t("targets_list_hint", count=len(targets))
    else:
        text += tr.t("targets_empty")
    if plan is not None:
        text += f"\nLimit: {len(targets)}/{plan.max_targets}"
    if not context.can_mutate:
        text += "\n\nRead-only: subscription is inactive."
    markup = build_subscriptions_keyboard(targets, tr, can_mutate=context.can_mutate)
    if isinstance(target, CallbackQuery):
        await _safe_edit(target, text, markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


async def _show_subscription_detail(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    tr: Translator,
    context: TenantContext,
    target_id: int,
) -> None:
    async with session_factory() as session:
        target = await SubscriptionRepository(session, context.tenant_id).get_target(target_id)

    if target is None:
        await callback.answer(tr.t("target_not_found"), show_alert=True)
        await _show_subscriptions(callback, session_factory, tr, context)
        return

    text = f"{tr.t('target_details_title')}\n\n{_subscription_line(target, tr)}"
    await _safe_edit(
        callback,
        text,
        build_subscription_detail_keyboard(
            target,
            tr,
            can_mutate=context.can_mutate,
        ),
    )
    await callback.answer()


async def _show_messages(
    target: Message | CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    tr: Translator,
    context: TenantContext,
) -> None:
    async with session_factory() as session:
        messages = await MessageRepository(session, context.tenant_id).list_messages()
        plan = await PlanRepository(session).get_active_plan()

    lines = [_message_line(item, tr) for item in messages] or [tr.t("messages_empty")]
    text = f"{tr.t('messages_title')}\n{tr.t('messages_hint')}\n\n" + "\n".join(lines)
    if plan is not None:
        text += f"\n\nLimit: {len(messages)}/{plan.max_templates}"
    if not context.can_mutate:
        text += "\nRead-only: subscription is inactive."
    markup = build_messages_keyboard(messages, tr, can_mutate=context.can_mutate)
    if isinstance(target, CallbackQuery):
        await _safe_edit(target, text, markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


async def _show_schedule(
    target: Message | CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    tr: Translator,
    context: TenantContext,
) -> None:
    async with session_factory() as session:
        settings = await SystemRepository(session, context.tenant_id).get_settings()
        plan = await PlanRepository(session).get_active_plan()

    text = (
        f"{tr.t('schedule_title')}\n"
        f"{tr.t('schedule_active')}: {settings.is_active}\n"
        f"{tr.t('schedule_interval')}: {settings.base_interval_minutes} min\n"
        f"{tr.t('schedule_jitter')}: {settings.jitter_minutes} min\n"
        f"{tr.t('schedule_paid_messages')}: {settings.allow_paid_messages}\n"
        f"{tr.t('schedule_paid_limit')}: {settings.max_paid_message_stars} Stars\n"
        f"{tr.t('schedule_last_run')}: {_dt(settings.last_broadcast_at, tr)}\n"
        f"{tr.t('schedule_next_run')}: {_dt(settings.next_broadcast_at, tr)}"
    )
    if plan is not None:
        text += f"\nMin interval by plan: {plan.min_interval_minutes} min"
    markup = build_schedule_keyboard(settings, tr, can_mutate=context.can_mutate)
    if isinstance(target, CallbackQuery):
        await _safe_edit(target, text, markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


async def _show_whitelist(
    target: Message | CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    tr: Translator,
    context: TenantContext,
) -> None:
    async with session_factory() as session:
        entries = await AccessRepository(session, context.tenant_id).list_whitelist()

    lines = [f"<code>{item.user_id}</code> - {html.escape(item.role)}" for item in entries] or [tr.t("whitelist_empty")]
    text = "<b>Tenant members</b>\n\n" + "\n".join(lines)
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
    context: TenantContext,
) -> None:
    async with session_factory() as session:
        summaries = await InboundRepository(session, context.tenant_id).list_sender_summaries()

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
    tr: Translator,
    context: TenantContext,
) -> None:
    async with session_factory() as session:
        snapshot = await StatusRepository(session, context.tenant_id).get_snapshot()

    failure_lines = (
        "\n".join(_failure_line(item, tr) for item in snapshot.recent_failures)
        if snapshot.recent_failures
        else tr.t("status_no_failures")
    )
    subscription_status = snapshot.subscription.status if snapshot.subscription else "inactive"
    lines = [
        tr.t("status_title"),
        f"Subscription: {html.escape(subscription_status)} until "
        f"{_dt(snapshot.subscription.current_period_end if snapshot.subscription else None, tr)}",
        f"{tr.t('status_total_targets')}: {snapshot.counts['total_targets']}",
        f"{tr.t('status_joined_targets')}: {snapshot.counts['joined_targets']}",
        f"{tr.t('status_active_messages')}: {snapshot.counts['active_messages']}",
        f"{tr.t('status_broadcast_active')}: {snapshot.settings.is_active}",
        f"{tr.t('schedule_last_run')}: {_dt(snapshot.settings.last_broadcast_at, tr)}",
        f"{tr.t('schedule_next_run')}: {_dt(snapshot.settings.next_broadcast_at, tr)}",
    ]
    if context.can_manage:
        lines[1:1] = [
            f"Tenant: <code>{context.tenant_id}</code>",
            f"{tr.t('status_owners')}: {', '.join(str(owner_id) for owner_id in snapshot.owner_ids) or 'none'}",
            f"{tr.t('status_session')}: {html.escape(snapshot.telegram_session.status)}",
            f"{tr.t('status_pending_joins')}: {snapshot.counts['pending_targets']}",
            f"{tr.t('status_whitelist_size')}: {snapshot.counts['whitelist_users']}",
        ]
        lines.extend(["", f"{tr.t('status_recent_failures')}\n{failure_lines}"])
    text = "\n".join(lines)
    markup = build_main_keyboard(
        tr,
        show_tenant_admin=context.can_manage,
        show_platform_admin=context.is_platform_admin,
    )
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
        context = await _ensure_message_access(message, session_factory, settings)
        if context is None:
            return
        await state.clear()
        tr = await _get_translator(session_factory, settings, message.from_user.id)
        await _show_main(message, tr, context)

    @router.message(Command("help"))
    async def help_command(message: Message, state: FSMContext) -> None:
        context = await _ensure_message_access(message, session_factory, settings)
        if context is None:
            return
        await state.clear()
        tr = await _get_translator(session_factory, settings, message.from_user.id)
        await _show_main(message, tr, context)

    @router.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext) -> None:
        context = await _ensure_message_access(message, session_factory, settings)
        if context is None:
            return
        await state.clear()
        tr = await _get_translator(session_factory, settings, message.from_user.id)
        await message.answer(
            tr.t("action_canceled"),
            reply_markup=build_main_keyboard(
                tr,
                show_tenant_admin=context.can_manage,
                show_platform_admin=context.is_platform_admin,
            ),
        )

    @router.callback_query(F.data == "noop")
    async def noop(callback: CallbackQuery) -> None:
        await callback.answer()

    @router.callback_query(F.data == "menu:main")
    async def menu_main(callback: CallbackQuery, state: FSMContext) -> None:
        context = await _ensure_callback_access(callback, session_factory, settings)
        if context is None:
            return
        await state.clear()
        tr = await _get_translator(session_factory, settings, callback.from_user.id)
        await _show_main(callback, tr, context)

    @router.callback_query(F.data == "menu:account")
    async def menu_account(callback: CallbackQuery, state: FSMContext) -> None:
        context = await _ensure_callback_access(callback, session_factory, settings)
        if context is None:
            return
        if not context.can_manage:
            await callback.answer("Tenant admin only.", show_alert=True)
            return
        await state.clear()
        tr = await _get_translator(session_factory, settings, callback.from_user.id)
        await _show_account(callback, session_factory, tr, context)

    @router.callback_query(F.data == "menu:billing")
    async def menu_billing(callback: CallbackQuery, state: FSMContext) -> None:
        context = await _ensure_callback_access(callback, session_factory, settings)
        if context is None:
            return
        if not context.can_manage:
            await callback.answer("Tenant admin only.", show_alert=True)
            return
        await state.clear()
        tr = await _get_translator(session_factory, settings, callback.from_user.id)
        await _show_billing(callback, session_factory, tr, context)

    @router.callback_query(F.data == "menu:admin")
    async def menu_admin(callback: CallbackQuery, state: FSMContext) -> None:
        context = await _ensure_callback_access(callback, session_factory, settings)
        if context is None:
            return
        await state.clear()
        tr = await _get_translator(session_factory, settings, callback.from_user.id)
        await _show_admin(callback, session_factory, tr, context)

    @router.callback_query(F.data == "menu:subscriptions")
    async def menu_subscriptions(callback: CallbackQuery, state: FSMContext) -> None:
        context = await _ensure_callback_access(callback, session_factory, settings)
        if context is None:
            return
        await state.clear()
        tr = await _get_translator(session_factory, settings, callback.from_user.id)
        await _show_subscriptions(callback, session_factory, tr, context)

    @router.callback_query(F.data == "menu:messages")
    async def menu_messages(callback: CallbackQuery, state: FSMContext) -> None:
        context = await _ensure_callback_access(callback, session_factory, settings)
        if context is None:
            return
        await state.clear()
        tr = await _get_translator(session_factory, settings, callback.from_user.id)
        await _show_messages(callback, session_factory, tr, context)

    @router.callback_query(F.data == "menu:schedule")
    async def menu_schedule(callback: CallbackQuery, state: FSMContext) -> None:
        context = await _ensure_callback_access(callback, session_factory, settings)
        if context is None:
            return
        await state.clear()
        tr = await _get_translator(session_factory, settings, callback.from_user.id)
        await _show_schedule(callback, session_factory, tr, context)

    @router.callback_query(F.data == "menu:whitelist")
    async def menu_whitelist(callback: CallbackQuery, state: FSMContext) -> None:
        context = await _ensure_callback_access(callback, session_factory, settings)
        if context is None:
            return
        if not context.can_manage:
            await callback.answer("Tenant admin only.", show_alert=True)
            return
        await state.clear()
        tr = await _get_translator(session_factory, settings, callback.from_user.id)
        await _show_whitelist(callback, session_factory, tr, context)

    @router.callback_query(F.data == "menu:inbound_users")
    async def menu_inbound_users(callback: CallbackQuery, state: FSMContext) -> None:
        context = await _ensure_callback_access(callback, session_factory, settings)
        if context is None:
            return
        await state.clear()
        tr = await _get_translator(session_factory, settings, callback.from_user.id)
        await _show_inbound_users(callback, session_factory, tr, context)

    @router.callback_query(F.data.startswith("inbound_user_no_link:"))
    async def inbound_user_no_link(callback: CallbackQuery) -> None:
        context = await _ensure_callback_access(callback, session_factory, settings)
        if context is None:
            return
        tr = await _get_translator(session_factory, settings, callback.from_user.id)
        sender_id = callback.data.split(":", 1)[1]
        await callback.answer(
            tr.t("inbound_user_no_link", user_id=sender_id),
            show_alert=True,
        )

    @router.callback_query(F.data == "menu:status")
    async def menu_status(callback: CallbackQuery, state: FSMContext) -> None:
        context = await _ensure_callback_access(callback, session_factory, settings)
        if context is None:
            return
        await state.clear()
        tr = await _get_translator(session_factory, settings, callback.from_user.id)
        await _show_status(callback, session_factory, tr, context)

    @router.callback_query(F.data == "menu:language")
    async def menu_language(callback: CallbackQuery, state: FSMContext) -> None:
        context = await _ensure_callback_access(callback, session_factory, settings)
        if context is None:
            return
        await state.clear()
        tr = await _get_translator(session_factory, settings, callback.from_user.id)
        await _show_language(callback, tr)

    @router.callback_query(F.data.startswith("lang:set:"))
    async def set_language(callback: CallbackQuery) -> None:
        context = await _ensure_callback_access(callback, session_factory, settings)
        if context is None:
            return
        language_code = callback.data.rsplit(":", 1)[1]
        async with session_factory() as session:
            await ManagerPreferenceRepository(session, context.tenant_id).set_language(
                callback.from_user.id,
                language_code,
            )
        tr = await _get_translator(session_factory, settings, callback.from_user.id)
        await _show_language(callback, tr, tr.t("language_updated"))

    @router.callback_query(F.data == "billing:pay")
    async def billing_pay(callback: CallbackQuery) -> None:
        context = await _ensure_callback_access(callback, session_factory, settings)
        if context is None:
            return
        if not context.can_manage:
            await callback.answer("Tenant admin only.", show_alert=True)
            return
        async with session_factory() as session:
            plan = await PlanRepository(session).get_active_plan()
        if plan is None:
            await callback.answer("No active plan.", show_alert=True)
            return
        payload = BillingRepository.build_payload(context.tenant_id, plan.id)
        try:
            invoice_link = await callback.bot.create_invoice_link(
                title=f"{plan.name} subscription",
                description=f"{plan.max_targets} targets, {plan.max_templates} templates",
                payload=payload,
                currency="XTR",
                prices=[LabeledPrice(label=plan.name, amount=plan.price_stars)],
                provider_token="",
                subscription_period=plan.period_seconds,
            )
        except TelegramBadRequest as exc:
            logger.warning(
                "Failed to create Stars invoice link for tenant %s: %s",
                context.tenant_id,
                exc,
            )
            await callback.answer(f"Invoice error: {exc.message}", show_alert=True)
            return
        await callback.message.answer(
            "Open the invoice link to pay with Telegram Stars.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=f"Pay {plan.price_stars} Stars",
                            url=invoice_link,
                        )
                    ]
                ]
            ),
        )
        await callback.answer()

    @router.pre_checkout_query()
    async def pre_checkout(query: PreCheckoutQuery) -> None:
        parsed = BillingRepository.parse_payload(query.invoice_payload)
        if parsed is None:
            await query.answer(ok=False, error_message="Invalid subscription payload.")
            return
        tenant_id, plan_id = parsed
        async with session_factory() as session:
            plan = await PlanRepository(session).get_plan(plan_id)
        if plan is None or not plan.is_active:
            await query.answer(ok=False, error_message="Plan is not available.")
            return
        if query.currency != "XTR" or query.total_amount != plan.price_stars:
            await query.answer(ok=False, error_message="Invoice amount changed.")
            return
        if tenant_id <= 0:
            await query.answer(ok=False, error_message="Invalid tenant.")
            return
        await query.answer(ok=True)

    @router.message(F.successful_payment)
    async def successful_payment(message: Message) -> None:
        context = await _ensure_message_access(message, session_factory, settings)
        if context is None:
            return
        payment = message.successful_payment
        parsed = BillingRepository.parse_payload(payment.invoice_payload)
        if parsed is None:
            await message.answer("Payment payload is invalid. Contact support.")
            return
        tenant_id, plan_id = parsed
        if tenant_id != context.tenant_id:
            await message.answer("Payment tenant does not match your account.")
            return
        async with session_factory() as session:
            subscription = await BillingRepository(session).activate_subscription(
                tenant_id=tenant_id,
                plan_id=plan_id,
                user_id=message.from_user.id,
                payload=payment.invoice_payload,
                currency=payment.currency,
                total_amount=payment.total_amount,
                telegram_payment_charge_id=payment.telegram_payment_charge_id,
                provider_payment_charge_id=payment.provider_payment_charge_id,
            )
        if subscription is None:
            await message.answer("Payment could not be applied. Contact support.")
            return
        await message.answer("Subscription activated.")

    @router.message(F.refunded_payment)
    async def refunded_payment(message: Message) -> None:
        context = await _ensure_message_access(message, session_factory, settings)
        if context is None:
            return
        refund = message.refunded_payment
        parsed = BillingRepository.parse_payload(refund.invoice_payload or "")
        if parsed is None:
            return
        tenant_id, plan_id = parsed
        if tenant_id != context.tenant_id:
            return
        async with session_factory() as session:
            await BillingRepository(session).record_refund(
                tenant_id=tenant_id,
                plan_id=plan_id,
                user_id=message.from_user.id,
                payload=refund.invoice_payload or "",
                currency=refund.currency,
                total_amount=refund.total_amount,
                telegram_payment_charge_id=refund.telegram_payment_charge_id,
                provider_payment_charge_id=refund.provider_payment_charge_id,
            )
        await message.answer("Payment refund recorded. Subscription is paused.")

    @router.callback_query(F.data == "account:connect")
    async def account_connect(callback: CallbackQuery, state: FSMContext) -> None:
        context = await _ensure_callback_access(callback, session_factory, settings)
        if context is None:
            return
        if not context.can_manage:
            await callback.answer("Tenant admin only.", show_alert=True)
            return
        if not settings.session_encryption_key:
            await callback.answer("SESSION_ENCRYPTION_KEY is not configured.", show_alert=True)
            return
        await state.set_state(ManagerStates.waiting_userbot_phone)
        await callback.message.answer("Send Telegram phone number in international format.")
        await callback.answer()

    @router.message(ManagerStates.waiting_userbot_phone, F.text)
    async def account_phone(message: Message, state: FSMContext) -> None:
        context = await _ensure_message_access(message, session_factory, settings)
        if context is None:
            return
        phone = message.text.strip()
        client = TelegramClient(StringSession(), settings.telegram_api_id, settings.telegram_api_hash)
        await client.connect()
        try:
            sent = await client.send_code_request(phone)
            await state.update_data(
                phone=phone,
                phone_code_hash=sent.phone_code_hash,
                string_session=client.session.save(),
            )
            await state.set_state(ManagerStates.waiting_userbot_code)
            await message.answer("Send login code from Telegram.")
        except Exception as exc:  # noqa: BLE001
            async with session_factory() as session:
                await TelegramSessionRepository(session, context.tenant_id).set_error(str(exc))
            await message.answer(f"Failed to send code: {html.escape(str(exc))}")
        finally:
            await client.disconnect()

    @router.message(ManagerStates.waiting_userbot_code, F.text)
    async def account_code(message: Message, state: FSMContext) -> None:
        context = await _ensure_message_access(message, session_factory, settings)
        if context is None:
            return
        data = await state.get_data()
        client = TelegramClient(
            StringSession(data["string_session"]),
            settings.telegram_api_id,
            settings.telegram_api_hash,
        )
        await client.connect()
        try:
            await client.sign_in(
                phone=data["phone"],
                code=message.text.strip(),
                phone_code_hash=data["phone_code_hash"],
            )
        except SessionPasswordNeededError:
            await state.update_data(string_session=client.session.save())
            await state.set_state(ManagerStates.waiting_userbot_password)
            await message.answer("Two-step verification is enabled. Send 2FA password.")
            await client.disconnect()
            return
        except Exception as exc:  # noqa: BLE001
            async with session_factory() as session:
                await TelegramSessionRepository(session, context.tenant_id).set_error(str(exc))
            await message.answer(f"Authorization failed: {html.escape(str(exc))}")
            await client.disconnect()
            return
        await _save_connected_userbot(client, session_factory, settings, context)
        await client.disconnect()
        await state.clear()
        await message.answer("Userbot connected.")

    @router.message(ManagerStates.waiting_userbot_password, F.text)
    async def account_password(message: Message, state: FSMContext) -> None:
        context = await _ensure_message_access(message, session_factory, settings)
        if context is None:
            return
        data = await state.get_data()
        client = TelegramClient(
            StringSession(data["string_session"]),
            settings.telegram_api_id,
            settings.telegram_api_hash,
        )
        await client.connect()
        try:
            await client.sign_in(password=message.text.strip())
            await _save_connected_userbot(client, session_factory, settings, context)
        except Exception as exc:  # noqa: BLE001
            async with session_factory() as session:
                await TelegramSessionRepository(session, context.tenant_id).set_error(str(exc))
            await message.answer(f"Authorization failed: {html.escape(str(exc))}")
            await client.disconnect()
            return
        await client.disconnect()
        await state.clear()
        await message.answer("Userbot connected.")

    @router.callback_query(F.data == "account:disconnect")
    async def account_disconnect(callback: CallbackQuery) -> None:
        context = await _ensure_callback_access(callback, session_factory, settings)
        if context is None:
            return
        if not context.can_manage:
            await callback.answer("Tenant admin only.", show_alert=True)
            return
        tr = await _get_translator(session_factory, settings, callback.from_user.id)
        async with session_factory() as session:
            await TelegramSessionRepository(session, context.tenant_id).disconnect()
        await _show_account(callback, session_factory, tr, context)

    @router.callback_query(F.data == "sub:add")
    async def add_subscription(callback: CallbackQuery, state: FSMContext) -> None:
        context = await _ensure_callback_access(callback, session_factory, settings)
        if context is None:
            return
        tr = await _get_translator(session_factory, settings, callback.from_user.id)
        if not await _require_mutation(callback, context, tr):
            return
        await state.set_state(ManagerStates.waiting_subscription_source)
        await callback.message.answer(tr.t("send_target"))
        await callback.answer()

    @router.callback_query(F.data.startswith("sub_view:"))
    async def view_subscription(callback: CallbackQuery, state: FSMContext) -> None:
        context = await _ensure_callback_access(callback, session_factory, settings)
        if context is None:
            return
        await state.clear()
        tr = await _get_translator(session_factory, settings, callback.from_user.id)
        target_id = int(callback.data.split(":")[1])
        await _show_subscription_detail(callback, session_factory, tr, context, target_id)

    @router.message(ManagerStates.waiting_subscription_source, F.text)
    async def subscription_source(message: Message, state: FSMContext) -> None:
        context = await _ensure_message_access(message, session_factory, settings)
        if context is None:
            return
        tr = await _get_translator(session_factory, settings, message.from_user.id)
        if not await _require_mutation(message, context, tr):
            return
        sources = split_target_sources(message.text)
        if not sources:
            await message.answer(tr.t("invalid_target", error="empty input"))
            return

        created_targets = []
        failed_sources: list[tuple[str, str]] = []
        async with session_factory() as session:
            repo = SubscriptionRepository(session, context.tenant_id)
            plan = await PlanRepository(session).get_active_plan()
            current_count = await repo.count_targets()
            if plan is not None and current_count + len(sources) > plan.max_targets:
                await message.answer(f"Target limit exceeded: {current_count}/{plan.max_targets}.")
                return
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
            tr.t("target_queued_line", id=target.id, source=html.escape(target.source))
            for target in created_targets
        ]
        response = [tr.t("targets_queued_summary", count=len(created_targets))]
        response.extend(queued_lines)
        await message.answer("\n".join(response))
        await _show_subscriptions(message, session_factory, tr, context)

    @router.callback_query(F.data.startswith("sub_toggle:"))
    async def toggle_subscription(callback: CallbackQuery) -> None:
        context = await _ensure_callback_access(callback, session_factory, settings)
        if context is None:
            return
        tr = await _get_translator(session_factory, settings, callback.from_user.id)
        if not await _require_mutation(callback, context, tr):
            return
        target_id = int(callback.data.split(":")[1])
        async with session_factory() as session:
            target = await SubscriptionRepository(session, context.tenant_id).toggle_enabled(target_id)
            if target is not None and target.is_enabled and target.is_joined:
                await SystemRepository(session, context.tenant_id).update_settings(next_broadcast_at=None)
        await _show_subscription_detail(callback, session_factory, tr, context, target_id)

    @router.callback_query(F.data.startswith("sub_retry:"))
    async def retry_subscription(callback: CallbackQuery) -> None:
        context = await _ensure_callback_access(callback, session_factory, settings)
        if context is None:
            return
        tr = await _get_translator(session_factory, settings, callback.from_user.id)
        if not await _require_mutation(callback, context, tr):
            return
        target_id = int(callback.data.split(":")[1])
        async with session_factory() as session:
            await SubscriptionRepository(session, context.tenant_id).queue_retry(target_id)
        await _show_subscription_detail(callback, session_factory, tr, context, target_id)

    @router.callback_query(F.data.startswith("sub_delete:"))
    async def delete_subscription(callback: CallbackQuery) -> None:
        context = await _ensure_callback_access(callback, session_factory, settings)
        if context is None:
            return
        tr = await _get_translator(session_factory, settings, callback.from_user.id)
        if not await _require_mutation(callback, context, tr):
            return
        target_id = int(callback.data.split(":")[1])
        async with session_factory() as session:
            await SubscriptionRepository(session, context.tenant_id).delete_target(target_id)
        await _show_subscriptions(callback, session_factory, tr, context)

    @router.callback_query(F.data == "msg:add")
    async def add_message(callback: CallbackQuery, state: FSMContext) -> None:
        context = await _ensure_callback_access(callback, session_factory, settings)
        if context is None:
            return
        tr = await _get_translator(session_factory, settings, callback.from_user.id)
        if not await _require_mutation(callback, context, tr):
            return
        await state.set_state(ManagerStates.waiting_message_text)
        await callback.message.answer(tr.t("send_message_text"))
        await callback.answer()

    @router.message(ManagerStates.waiting_message_text, F.text)
    async def create_message(message: Message, state: FSMContext) -> None:
        context = await _ensure_message_access(message, session_factory, settings)
        if context is None:
            return
        tr = await _get_translator(session_factory, settings, message.from_user.id)
        if not await _require_mutation(message, context, tr):
            return
        async with session_factory() as session:
            repo = MessageRepository(session, context.tenant_id)
            plan = await PlanRepository(session).get_active_plan()
            current_count = await repo.count_messages()
            if plan is not None and current_count >= plan.max_templates:
                await message.answer(f"Template limit exceeded: {current_count}/{plan.max_templates}.")
                return
            created = await repo.create_message(message.text, message.from_user.id)
            await SystemRepository(session, context.tenant_id).update_settings(next_broadcast_at=None)
        await state.clear()
        await message.answer(tr.t("message_saved", id=created.id))
        await _show_messages(message, session_factory, tr, context)

    @router.callback_query(F.data.startswith("msg_toggle:"))
    async def toggle_message(callback: CallbackQuery) -> None:
        context = await _ensure_callback_access(callback, session_factory, settings)
        if context is None:
            return
        tr = await _get_translator(session_factory, settings, callback.from_user.id)
        if not await _require_mutation(callback, context, tr):
            return
        message_id = int(callback.data.split(":")[1])
        async with session_factory() as session:
            message = await MessageRepository(session, context.tenant_id).toggle_message(message_id)
            if message is not None and message.is_enabled:
                await SystemRepository(session, context.tenant_id).update_settings(next_broadcast_at=None)
        await _show_messages(callback, session_factory, tr, context)

    @router.callback_query(F.data.startswith("msg_delete:"))
    async def delete_message(callback: CallbackQuery) -> None:
        context = await _ensure_callback_access(callback, session_factory, settings)
        if context is None:
            return
        tr = await _get_translator(session_factory, settings, callback.from_user.id)
        if not await _require_mutation(callback, context, tr):
            return
        message_id = int(callback.data.split(":")[1])
        async with session_factory() as session:
            await MessageRepository(session, context.tenant_id).delete_message(message_id)
        await _show_messages(callback, session_factory, tr, context)

    @router.callback_query(F.data == "schedule:set_interval")
    async def schedule_set_interval(callback: CallbackQuery, state: FSMContext) -> None:
        context = await _ensure_callback_access(callback, session_factory, settings)
        if context is None:
            return
        tr = await _get_translator(session_factory, settings, callback.from_user.id)
        if not await _require_mutation(callback, context, tr):
            return
        await state.set_state(ManagerStates.waiting_interval)
        await callback.message.answer(tr.t("send_interval"))
        await callback.answer()

    @router.message(ManagerStates.waiting_interval, F.text)
    async def schedule_interval(message: Message, state: FSMContext) -> None:
        context = await _ensure_message_access(message, session_factory, settings)
        if context is None:
            return
        tr = await _get_translator(session_factory, settings, message.from_user.id)
        if not await _require_mutation(message, context, tr):
            return
        try:
            value = max(1, int(message.text.strip()))
        except ValueError:
            await message.answer(tr.t("invalid_interval"))
            return
        async with session_factory() as session:
            plan = await PlanRepository(session).get_active_plan()
            if plan is not None and value < plan.min_interval_minutes:
                await message.answer(f"Minimum interval for the plan is {plan.min_interval_minutes} minutes.")
                return
            repo = SystemRepository(session, context.tenant_id)
            current = await repo.get_settings()
            jitter = current.jitter_minutes if current else settings.default_jitter_minutes
            next_run = compute_next_broadcast_time(
                now=datetime.now(timezone.utc),
                base_interval_minutes=value,
                jitter_minutes=jitter,
            )
            await repo.update_settings(base_interval_minutes=value, next_broadcast_at=next_run)
        await state.clear()
        await _show_schedule(message, session_factory, tr, context)

    @router.callback_query(F.data == "schedule:set_jitter")
    async def schedule_set_jitter(callback: CallbackQuery, state: FSMContext) -> None:
        context = await _ensure_callback_access(callback, session_factory, settings)
        if context is None:
            return
        tr = await _get_translator(session_factory, settings, callback.from_user.id)
        if not await _require_mutation(callback, context, tr):
            return
        await state.set_state(ManagerStates.waiting_jitter)
        await callback.message.answer(tr.t("send_jitter"))
        await callback.answer()

    @router.message(ManagerStates.waiting_jitter, F.text)
    async def schedule_jitter(message: Message, state: FSMContext) -> None:
        context = await _ensure_message_access(message, session_factory, settings)
        if context is None:
            return
        tr = await _get_translator(session_factory, settings, message.from_user.id)
        if not await _require_mutation(message, context, tr):
            return
        try:
            value = max(0, int(message.text.strip()))
        except ValueError:
            await message.answer(tr.t("invalid_jitter"))
            return
        async with session_factory() as session:
            repo = SystemRepository(session, context.tenant_id)
            current = await repo.get_settings()
            interval = current.base_interval_minutes if current else settings.default_interval_minutes
            next_run = compute_next_broadcast_time(
                now=datetime.now(timezone.utc),
                base_interval_minutes=interval,
                jitter_minutes=value,
            )
            await repo.update_settings(jitter_minutes=value, next_broadcast_at=next_run)
        await state.clear()
        await _show_schedule(message, session_factory, tr, context)

    @router.callback_query(F.data == "schedule:set_paid_stars")
    async def schedule_set_paid_stars(callback: CallbackQuery, state: FSMContext) -> None:
        context = await _ensure_callback_access(callback, session_factory, settings)
        if context is None:
            return
        tr = await _get_translator(session_factory, settings, callback.from_user.id)
        if not await _require_mutation(callback, context, tr):
            return
        await state.set_state(ManagerStates.waiting_paid_stars)
        await callback.message.answer(tr.t("send_paid_stars"))
        await callback.answer()

    @router.message(ManagerStates.waiting_paid_stars, F.text)
    async def schedule_paid_stars(message: Message, state: FSMContext) -> None:
        context = await _ensure_message_access(message, session_factory, settings)
        if context is None:
            return
        tr = await _get_translator(session_factory, settings, message.from_user.id)
        try:
            value = int(message.text.strip())
        except ValueError:
            await message.answer(tr.t("invalid_paid_stars"))
            return
        if value < 0 or value > 10000:
            await message.answer(tr.t("invalid_paid_stars"))
            return
        async with session_factory() as session:
            await SystemRepository(session, context.tenant_id).update_settings(max_paid_message_stars=value)
        await state.clear()
        await _show_schedule(message, session_factory, tr, context)

    @router.callback_query(F.data == "schedule:toggle_paid_messages")
    async def schedule_toggle_paid_messages(callback: CallbackQuery) -> None:
        context = await _ensure_callback_access(callback, session_factory, settings)
        if context is None:
            return
        tr = await _get_translator(session_factory, settings, callback.from_user.id)
        if not await _require_mutation(callback, context, tr):
            return
        async with session_factory() as session:
            repo = SystemRepository(session, context.tenant_id)
            current = await repo.get_settings()
            await repo.update_settings(allow_paid_messages=not current.allow_paid_messages)
        await _show_schedule(callback, session_factory, tr, context)

    @router.callback_query(F.data == "schedule:toggle")
    async def schedule_toggle(callback: CallbackQuery) -> None:
        context = await _ensure_callback_access(callback, session_factory, settings)
        if context is None:
            return
        tr = await _get_translator(session_factory, settings, callback.from_user.id)
        if not await _require_mutation(callback, context, tr):
            return
        async with session_factory() as session:
            repo = SystemRepository(session, context.tenant_id)
            current = await repo.get_settings()
            will_activate = not current.is_active
            await repo.update_settings(
                is_active=will_activate,
                next_broadcast_at=None if will_activate else current.next_broadcast_at,
            )
        await _show_schedule(callback, session_factory, tr, context)

    @router.callback_query(F.data == "wl:add")
    async def whitelist_add(callback: CallbackQuery, state: FSMContext) -> None:
        context = await _ensure_callback_access(callback, session_factory, settings)
        if context is None:
            return
        tr = await _get_translator(session_factory, settings, callback.from_user.id)
        if not context.can_manage:
            await callback.answer(tr.t("access_denied"), show_alert=True)
            return
        await state.set_state(ManagerStates.waiting_whitelist_add)
        await callback.message.answer(tr.t("send_whitelist_add"))
        await callback.answer()

    @router.message(ManagerStates.waiting_whitelist_add, F.text)
    async def whitelist_add_value(message: Message, state: FSMContext) -> None:
        context = await _ensure_message_access(message, session_factory, settings)
        if context is None:
            return
        tr = await _get_translator(session_factory, settings, message.from_user.id)
        try:
            user_id = int(message.text.strip())
        except ValueError:
            await message.answer(tr.t("invalid_user_id"))
            return
        async with session_factory() as session:
            await AccessRepository(session, context.tenant_id).add_whitelist_user(user_id)
        await state.clear()
        await _show_whitelist(message, session_factory, tr, context)

    @router.callback_query(F.data == "wl:remove")
    async def whitelist_remove(callback: CallbackQuery, state: FSMContext) -> None:
        context = await _ensure_callback_access(callback, session_factory, settings)
        if context is None:
            return
        tr = await _get_translator(session_factory, settings, callback.from_user.id)
        if not context.can_manage:
            await callback.answer(tr.t("access_denied"), show_alert=True)
            return
        await state.set_state(ManagerStates.waiting_whitelist_remove)
        await callback.message.answer(tr.t("send_whitelist_remove"))
        await callback.answer()

    @router.message(ManagerStates.waiting_whitelist_remove, F.text)
    async def whitelist_remove_value(message: Message, state: FSMContext) -> None:
        context = await _ensure_message_access(message, session_factory, settings)
        if context is None:
            return
        tr = await _get_translator(session_factory, settings, message.from_user.id)
        try:
            user_id = int(message.text.strip())
        except ValueError:
            await message.answer(tr.t("invalid_user_id"))
            return
        async with session_factory() as session:
            await AccessRepository(session, context.tenant_id).remove_whitelist_user(user_id)
        await state.clear()
        await _show_whitelist(message, session_factory, tr, context)

    async def _admin_prompt(callback: CallbackQuery, state: FSMContext, next_state: State, prompt: str) -> None:
        context = await _ensure_callback_access(callback, session_factory, settings)
        if context is None:
            return
        if not context.is_platform_admin:
            await callback.answer("Platform admin only.", show_alert=True)
            return
        await state.set_state(next_state)
        await callback.message.answer(prompt)
        await callback.answer()

    @router.callback_query(F.data == "admin:set_price")
    async def admin_set_price(callback: CallbackQuery, state: FSMContext) -> None:
        await _admin_prompt(callback, state, ManagerStates.waiting_admin_price, "Send price in Stars (1..10000).")

    @router.callback_query(F.data == "admin:set_max_targets")
    async def admin_set_max_targets(callback: CallbackQuery, state: FSMContext) -> None:
        await _admin_prompt(callback, state, ManagerStates.waiting_admin_max_targets, "Send max targets.")

    @router.callback_query(F.data == "admin:set_max_templates")
    async def admin_set_max_templates(callback: CallbackQuery, state: FSMContext) -> None:
        await _admin_prompt(callback, state, ManagerStates.waiting_admin_max_templates, "Send max templates.")

    @router.callback_query(F.data == "admin:set_min_interval")
    async def admin_set_min_interval(callback: CallbackQuery, state: FSMContext) -> None:
        await _admin_prompt(callback, state, ManagerStates.waiting_admin_min_interval, "Send min interval in minutes.")

    @router.callback_query(F.data == "admin:toggle_plan")
    async def admin_toggle_plan(callback: CallbackQuery) -> None:
        context = await _ensure_callback_access(callback, session_factory, settings)
        if context is None or not context.is_platform_admin:
            await callback.answer("Platform admin only.", show_alert=True)
            return
        tr = await _get_translator(session_factory, settings, callback.from_user.id)
        async with session_factory() as session:
            plan = await PlanRepository(session).get_active_plan()
            if plan is None:
                plan = await TenantRepository(session).ensure_default_plan()
            await PlanRepository(session).update_active_plan(is_active=not plan.is_active)
        await _show_admin(callback, session_factory, tr, context)

    async def _handle_admin_int(
        message: Message,
        state: FSMContext,
        *,
        field: str,
    ) -> None:
        context = await _ensure_message_access(message, session_factory, settings)
        if context is None or not context.is_platform_admin:
            return
        try:
            value = int(message.text.strip())
        except ValueError:
            await message.answer("Send an integer.")
            return
        kwargs = {field: value}
        async with session_factory() as session:
            await PlanRepository(session).update_active_plan(**kwargs)
        await state.clear()
        tr = await _get_translator(session_factory, settings, message.from_user.id)
        await message.answer("Plan updated.")
        await _show_admin(message, session_factory, tr, context)

    @router.message(ManagerStates.waiting_admin_price, F.text)
    async def admin_price_value(message: Message, state: FSMContext) -> None:
        await _handle_admin_int(message, state, field="price_stars")

    @router.message(ManagerStates.waiting_admin_max_targets, F.text)
    async def admin_max_targets_value(message: Message, state: FSMContext) -> None:
        await _handle_admin_int(message, state, field="max_targets")

    @router.message(ManagerStates.waiting_admin_max_templates, F.text)
    async def admin_max_templates_value(message: Message, state: FSMContext) -> None:
        await _handle_admin_int(message, state, field="max_templates")

    @router.message(ManagerStates.waiting_admin_min_interval, F.text)
    async def admin_min_interval_value(message: Message, state: FSMContext) -> None:
        await _handle_admin_int(message, state, field="min_interval_minutes")

    @router.message(F.text)
    async def fallback(message: Message) -> None:
        context = await _ensure_message_access(message, session_factory, settings)
        if context is None:
            return
        tr = await _get_translator(session_factory, settings, message.from_user.id)
        await message.answer(tr.t("fallback"))

    return router


async def _save_connected_userbot(
    client: TelegramClient,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    context: TenantContext,
) -> None:
    me = await client.get_me()
    string_session = client.session.save()
    encrypted = SessionCipher(settings.session_encryption_key).encrypt(string_session)
    async with session_factory() as session:
        await TelegramSessionRepository(session, context.tenant_id).save_connected(
            encrypted_string_session=encrypted,
            telegram_user_id=getattr(me, "id", None),
        )


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
