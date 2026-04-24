from __future__ import annotations

import html

from tg_spam_agent.models import InboundEvent


def build_inbound_notification(event: InboundEvent) -> str:
    preview = html.escape(event.message_preview or "<no text>")
    username = html.escape(f"@{event.username}" if event.username else "n/a")
    full_name = html.escape(event.full_name or "Unknown user")
    return (
        "<b>Sender userbot got a new private message.</b>\n\n"
        f"User ID: <code>{event.sender_id}</code>\n"
        f"Username: {username}\n"
        f"Name: {full_name}\n"
        f"Type: {event.message_type}\n"
        f"Preview: {preview}"
    )
