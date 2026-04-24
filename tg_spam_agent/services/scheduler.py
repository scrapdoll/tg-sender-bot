from __future__ import annotations

from datetime import datetime, timedelta, timezone
from random import Random


def compute_next_broadcast_time(
    *,
    now: datetime | None,
    base_interval_minutes: int,
    jitter_minutes: int,
    rng: Random | None = None,
) -> datetime:
    current = now or datetime.now(timezone.utc)
    chooser = rng or Random()
    jitter = chooser.randint(0, max(jitter_minutes, 0))
    return current + timedelta(minutes=max(base_interval_minutes, 1) + jitter)
