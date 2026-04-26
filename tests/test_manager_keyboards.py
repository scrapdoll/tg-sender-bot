from __future__ import annotations

from tg_spam_agent.manager.i18n import Translator
from tg_spam_agent.manager.keyboards import build_inbound_users_keyboard
from tg_spam_agent.models import InboundEvent
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
