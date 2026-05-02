from __future__ import annotations

from tg_spam_agent.manager.i18n import Translator
from tg_spam_agent.manager.keyboards import (
    build_inbound_users_keyboard,
    build_main_keyboard,
    build_messages_keyboard,
    build_schedule_keyboard,
    build_subscriptions_keyboard,
)
from tg_spam_agent.models import BroadcastSettings, InboundEvent, MessageTemplate
from tg_spam_agent.repositories import InboundSenderSummary


def test_inbound_user_keyboard_uses_tme_link_for_username() -> None:
    summary = InboundSenderSummary(
        event=InboundEvent(sender_id=1001, username="valid_user", message_type="text"),
        message_count=2,
    )

    markup = build_inbound_users_keyboard([summary], Translator("en"))

    button = markup.inline_keyboard[0][0]
    assert button.url == "https://t.me/valid_user"
    assert button.callback_data is None


def test_inbound_user_keyboard_avoids_tg_user_link_without_username() -> None:
    summary = InboundSenderSummary(
        event=InboundEvent(sender_id=1001, username=None, message_type="text"),
        message_count=2,
    )

    markup = build_inbound_users_keyboard([summary], Translator("en"))

    button = markup.inline_keyboard[0][0]
    assert button.url is None
    assert button.callback_data == "inbound_user_no_link:1001"


def test_main_keyboard_hides_admin_only_buttons_for_regular_users() -> None:
    markup = build_main_keyboard(
        Translator("en"),
        show_tenant_admin=False,
        show_platform_admin=False,
    )
    labels = [button.text for row in markup.inline_keyboard for button in row]

    assert "Admin" not in labels
    assert "Account" not in labels
    assert "Subscription" not in labels
    assert "Whitelist" not in labels


def test_main_keyboard_shows_admin_button_for_platform_admins() -> None:
    markup = build_main_keyboard(
        Translator("en"),
        show_tenant_admin=True,
        show_platform_admin=True,
    )
    labels = [button.text for row in markup.inline_keyboard for button in row]

    assert "Admin" in labels
    assert "Account" in labels
    assert "Subscription" in labels
    assert "Whitelist" in labels


def test_read_only_keyboards_hide_mutation_buttons() -> None:
    tr = Translator("en")
    target_markup = build_subscriptions_keyboard([], tr, can_mutate=False)
    message_markup = build_messages_keyboard(
        [MessageTemplate(id=1, tenant_id=1, text="hello", created_by=1)],
        tr,
        can_mutate=False,
    )
    schedule_markup = build_schedule_keyboard(
        BroadcastSettings(tenant_id=1),
        tr,
        can_mutate=False,
    )

    labels = [
        button.text
        for markup in (target_markup, message_markup, schedule_markup)
        for row in markup.inline_keyboard
        for button in row
    ]

    assert "Add target" not in labels
    assert "Add message" not in labels
    assert "Set interval" not in labels
