from __future__ import annotations

from random import Random

from tg_spam_agent.models import MessageTemplate


def pick_random_message(
    messages: list[MessageTemplate], rng: Random | None = None
) -> MessageTemplate | None:
    if not messages:
        return None
    chooser = rng or Random()
    return chooser.choice(messages)
