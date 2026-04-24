from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(slots=True)
class ParsedSource:
    normalized: str
    access_type: str
    lookup_value: str


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

        username = path.split("/", 1)[0]
        return ParsedSource(
            normalized=f"@{username.lower()}",
            access_type="public",
            lookup_value=username,
        )

    raise ValueError("Use @username, public t.me link, or invite link.")
