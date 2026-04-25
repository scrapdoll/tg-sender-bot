from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(slots=True)
class ParsedSource:
    normalized: str
    access_type: str
    lookup_value: str
    topic_id: int | None = None


def parse_target_source(raw_text: str) -> ParsedSource:
    value = raw_text.strip()
    if not value:
        raise ValueError("Empty source value.")

    if value.startswith("@"):
        username = value[1:].strip()
        if not username:
            raise ValueError("Username is empty.")
        return ParsedSource(
            normalized=f"@{username.lower()}",
            access_type="public",
            lookup_value=username,
        )

    if value.startswith("http://") or value.startswith("https://"):
        parsed = urlparse(value)
        if parsed.netloc not in {"t.me", "telegram.me", "www.t.me"}:
            raise ValueError("Only t.me and telegram.me links are supported.")

        path = parsed.path.strip("/")
        if not path:
            raise ValueError("Telegram link path is empty.")

        if path.startswith("+"):
            invite_hash = path.removeprefix("+")
            if not invite_hash:
                raise ValueError("Invite hash is empty.")
            return ParsedSource(
                normalized=f"invite:{invite_hash}",
                access_type="private_invite",
                lookup_value=invite_hash,
            )

        if path.startswith("joinchat/"):
            invite_hash = path.split("/", 1)[1]
            if not invite_hash:
                raise ValueError("Invite hash is empty.")
            return ParsedSource(
                normalized=f"invite:{invite_hash}",
                access_type="private_invite",
                lookup_value=invite_hash,
            )

        parts = path.split("/")
        if parts[0] == "c" and len(parts) >= 3:
            internal_chat_id = parts[1]
            topic_id = parts[2]
            if not internal_chat_id.isdigit() or not topic_id.isdigit():
                raise ValueError("Private topic link must contain numeric chat and topic IDs.")
            peer_id = int(f"-100{internal_chat_id}")
            return ParsedSource(
                normalized=f"c:{internal_chat_id}/{topic_id}",
                access_type="private_topic",
                lookup_value=str(peer_id),
                topic_id=int(topic_id),
            )

        username = parts[0]
        if len(parts) >= 2 and parts[1].isdigit():
            topic_id = int(parts[1])
            return ParsedSource(
                normalized=f"@{username.lower()}/{topic_id}",
                access_type="public_topic",
                lookup_value=username,
                topic_id=topic_id,
            )

        return ParsedSource(
            normalized=f"@{username.lower()}",
            access_type="public",
            lookup_value=username,
        )

    raise ValueError("Use @username, public t.me link, or invite link.")
