from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from tg_spam_agent.manager.i18n import Translator
from tg_spam_agent.models import BroadcastSettings, MessageTemplate, SubscriptionTarget
from tg_spam_agent.repositories import InboundSenderSummary


def _shorten(text: str, limit: int = 24) -> str:
    return text if len(text) <= limit else f"{text[: limit - 1]}..."


def build_main_keyboard(tr: Translator) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=tr.t("btn_targets"), callback_data="menu:subscriptions")
    builder.button(text=tr.t("btn_messages"), callback_data="menu:messages")
    builder.button(text=tr.t("btn_schedule"), callback_data="menu:schedule")
    builder.button(text=tr.t("btn_inbound_users"), callback_data="menu:inbound_users")
    builder.button(text=tr.t("btn_whitelist"), callback_data="menu:whitelist")
    builder.button(text=tr.t("btn_status"), callback_data="menu:status")
    builder.button(text=tr.t("btn_language"), callback_data="menu:language")
    builder.adjust(2, 2, 2, 1)
    return builder.as_markup()


def build_subscriptions_keyboard(
    targets: list[SubscriptionTarget],
    tr: Translator,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=tr.t("btn_add_target"), callback_data="sub:add")
    builder.button(text=tr.t("btn_refresh"), callback_data="menu:subscriptions")
    for target in targets:
        label = _target_button_label(target)
        builder.button(
            text=label,
            callback_data=f"sub_view:{target.id}",
        )
    builder.button(text=tr.t("btn_back"), callback_data="menu:main")
    builder.adjust(2, repeat=True)
    return builder.as_markup()


def _target_button_label(target: SubscriptionTarget) -> str:
    status = "on" if target.is_enabled else "off"
    joined = "ok" if target.is_joined else target.join_status
    label = _shorten(target.title or target.source, 34)
    return f"#{target.id} {label} [{status}/{joined}]"


def build_subscription_detail_keyboard(
    target: SubscriptionTarget,
    tr: Translator,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=tr.t("btn_disable") if target.is_enabled else tr.t("btn_enable"),
        callback_data=f"sub_toggle:{target.id}",
    )
    builder.button(text=tr.t("btn_retry"), callback_data=f"sub_retry:{target.id}")
    builder.button(text=tr.t("btn_delete"), callback_data=f"sub_delete:{target.id}")
    builder.button(text=tr.t("btn_back"), callback_data="menu:subscriptions")
    builder.adjust(2, 1, 1)
    return builder.as_markup()


def build_messages_keyboard(
    messages: list[MessageTemplate],
    tr: Translator,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=tr.t("btn_add_message"), callback_data="msg:add")
    builder.button(text=tr.t("btn_refresh"), callback_data="menu:messages")
    for message in messages:
        builder.button(
            text=f"{tr.t('btn_disable') if message.is_enabled else tr.t('btn_enable')} #{message.id}",
            callback_data=f"msg_toggle:{message.id}",
        )
        builder.button(
            text=f"{tr.t('btn_delete')} #{message.id}",
            callback_data=f"msg_delete:{message.id}",
        )
    builder.button(text=tr.t("btn_back"), callback_data="menu:main")
    builder.adjust(2, repeat=True)
    return builder.as_markup()


def build_schedule_keyboard(
    settings: BroadcastSettings,
    tr: Translator,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=tr.t("btn_set_interval"), callback_data="schedule:set_interval")
    builder.button(text=tr.t("btn_set_jitter"), callback_data="schedule:set_jitter")
    builder.button(
        text=tr.t("btn_disable_sender") if settings.is_active else tr.t("btn_enable_sender"),
        callback_data="schedule:toggle",
    )
    builder.button(text=tr.t("btn_back"), callback_data="menu:main")
    builder.adjust(2, 1, 1)
    return builder.as_markup()


def build_whitelist_keyboard(tr: Translator) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=tr.t("btn_add_id"), callback_data="wl:add")
    builder.button(text=tr.t("btn_remove_id"), callback_data="wl:remove")
    builder.button(text=tr.t("btn_refresh"), callback_data="menu:whitelist")
    builder.button(text=tr.t("btn_back"), callback_data="menu:main")
    builder.adjust(2, 1, 1)
    return builder.as_markup()


def build_inbound_users_keyboard(
    summaries: list[InboundSenderSummary],
    tr: Translator,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for summary in summaries:
        builder.button(
            text=_inbound_user_button_label(summary, tr),
            url=_inbound_user_url(summary),
        )
    builder.button(text=tr.t("btn_refresh"), callback_data="menu:inbound_users")
    builder.button(text=tr.t("btn_back"), callback_data="menu:main")
    builder.adjust(*([1] * len(summaries)), 2)
    return builder.as_markup()


def _inbound_user_button_label(summary: InboundSenderSummary, tr: Translator) -> str:
    event = summary.event
    label = event.full_name or (f"@{event.username}" if event.username else None)
    if not label:
        label = f"{tr.t('inbound_unknown_user')} {event.sender_id}"
    return f"{_shorten(label, 36)} ({summary.message_count})"


def _inbound_user_url(summary: InboundSenderSummary) -> str:
    if summary.event.username:
        return f"https://t.me/{summary.event.username}"
    return f"tg://user?id={summary.event.sender_id}"


def build_language_keyboard(tr: Translator) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=tr.t("language_ru"), callback_data="lang:set:ru")
    builder.button(text=tr.t("language_en"), callback_data="lang:set:en")
    builder.button(text=tr.t("btn_back"), callback_data="menu:main")
    builder.adjust(2, 1)
    return builder.as_markup()
