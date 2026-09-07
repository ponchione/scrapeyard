"""Job-run lease checks used by the scheduler."""

from __future__ import annotations

from datetime import datetime


def run_lease_is_active(
    updated_at: datetime | None,
    *,
    lease_seconds: int,
    now: datetime,
) -> bool:
    """Return True when a queued/running job timestamp is still leased."""
    if updated_at is None:
        return False
    comparable_now = now.replace(tzinfo=None) if updated_at.tzinfo is None else now
    return (comparable_now - updated_at).total_seconds() < lease_seconds
