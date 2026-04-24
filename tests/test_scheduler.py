from __future__ import annotations

from datetime import datetime, timezone
from random import Random

from tg_spam_agent.services.scheduler import compute_next_broadcast_time


def test_compute_next_broadcast_time_uses_interval_and_jitter() -> None:
    now = datetime(2026, 4, 23, 18, 0, tzinfo=timezone.utc)
    result = compute_next_broadcast_time(
        now=now,
        base_interval_minutes=10,
        jitter_minutes=5,
        rng=Random(1),
    )

    assert result == datetime(2026, 4, 23, 18, 11, tzinfo=timezone.utc)
