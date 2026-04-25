from __future__ import annotations

from datetime import datetime, timedelta, timezone
from random import Random

from tg_spam_agent.services.datetime_utils import ensure_utc


def compute_next_broadcast_time(
    *,
    now: datetime | None,
    base_interval_minutes: int,
    jitter_minutes: int,
    rng: Random | None = None,
) -> datetime:
    current = ensure_utc(now) or datetime.now(timezone.utc)
    chooser = rng or Random()
    jitter = chooser.randint(0, max(jitter_minutes, 0))
    return current + timedelta(minutes=max(base_interval_minutes, 1) + jitter)


def is_broadcast_due(next_broadcast_at: datetime | None, now: datetime | None = None) -> bool:
    next_run = ensure_utc(next_broadcast_at)
    if next_run is None:
        return True
    current = ensure_utc(now) or datetime.now(timezone.utc)
    return next_run <= current
