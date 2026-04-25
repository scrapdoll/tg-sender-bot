from __future__ import annotations

from datetime import datetime, timezone
from random import Random

from tg_spam_agent.services.scheduler import compute_next_broadcast_time, is_broadcast_due


def test_compute_next_broadcast_time_uses_interval_and_jitter() -> None:
    now = datetime(2026, 4, 23, 18, 0, tzinfo=timezone.utc)
    result = compute_next_broadcast_time(
        now=now,
        base_interval_minutes=10,
        jitter_minutes=5,
        rng=Random(1),
    )

    assert result == datetime(2026, 4, 23, 18, 11, tzinfo=timezone.utc)


def test_broadcast_due_handles_naive_sqlite_datetime() -> None:
    next_run_from_sqlite = datetime(2026, 4, 23, 18, 0)
    now = datetime(2026, 4, 23, 18, 1, tzinfo=timezone.utc)

    assert is_broadcast_due(next_run_from_sqlite, now) is True


def test_broadcast_not_due_handles_naive_sqlite_datetime() -> None:
    next_run_from_sqlite = datetime(2026, 4, 23, 18, 2)
    now = datetime(2026, 4, 23, 18, 1, tzinfo=timezone.utc)

    assert is_broadcast_due(next_run_from_sqlite, now) is False
