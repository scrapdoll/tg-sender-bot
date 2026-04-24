from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from tg_spam_agent.manager.i18n import Translator
from tg_spam_agent.models import BroadcastSettings, MessageTemplate, SubscriptionTarget


def _shorten(text: str, limit: int = 24) -> str:
    return text if len(text) <= limit else f"{text[: limit - 1]}..."


def build_main_keyboard(tr: Translator) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=tr.t("btn_targets"), callback_data="menu:subscriptions")
    builder.button(text=tr.t("btn_messages"), callback_data="menu:messages")
    builder.button(text=tr.t("btn_schedule"), callback_data="menu:schedule")
    builder.button(text=tr.t("btn_whitelist"), callback_data="menu:whitelist")
    builder.button(text=tr.t("btn_status"), callback_data="menu:status")
    builder.button(text=tr.t("btn_language"), callback_data="menu:language")
    builder.adjust(2, 2, 2)
    return builder.as_markup()


def build_subscriptions_keyboard(
    targets: list[SubscriptionTarget],
    tr: Translator,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=tr.t("btn_add_target"), callback_data="sub:add")
    builder.button(text=tr.t("btn_refresh"), callback_data="menu:subscriptions")
    for target in targets:
        label = _shorten(target.title or target.source)
        builder.button(
            text=f"{tr.t('btn_disable') if target.is_enabled else tr.t('btn_enable')} #{target.id}",
            callback_data=f"sub_toggle:{target.id}",
        )
        builder.button(
            text=f"{tr.t('btn_retry')} #{target.id}",
            callback_data=f"sub_retry:{target.id}",
        )
        builder.button(
            text=f"{tr.t('btn_delete')} #{target.id}",
            callback_data=f"sub_delete:{target.id}",
        )
        builder.button(text=f"Info: {label}", callback_data="noop")
    builder.button(text=tr.t("btn_back"), callback_data="menu:main")
    builder.adjust(2, 1, 1, repeat=True)
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


def build_language_keyboard(tr: Translator) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=tr.t("language_ru"), callback_data="lang:set:ru")
    builder.button(text=tr.t("language_en"), callback_data="lang:set:en")
    builder.button(text=tr.t("btn_back"), callback_data="menu:main")
    builder.adjust(2, 1)
    return builder.as_markup()
