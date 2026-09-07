from __future__ import annotations

from datetime import datetime, timezone

from scrapeyard.queue.job_state import run_lease_is_active


def test_run_lease_is_active_for_recent_timestamp():
    now = datetime(2026, 4, 9, 12, 10, tzinfo=timezone.utc)
    updated_at = datetime(2026, 4, 9, 12, 6, tzinfo=timezone.utc)

    assert run_lease_is_active(updated_at, lease_seconds=300, now=now) is True


def test_run_lease_is_not_active_for_stale_or_missing_timestamp():
    now = datetime(2026, 4, 9, 12, 10, tzinfo=timezone.utc)

    assert run_lease_is_active(None, lease_seconds=300, now=now) is False
    assert run_lease_is_active(
        datetime(2026, 4, 9, 12, 4, tzinfo=timezone.utc),
        lease_seconds=300,
        now=now,
    ) is False
