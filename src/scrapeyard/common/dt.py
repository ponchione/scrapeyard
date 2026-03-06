"""Shared datetime formatting helpers for storage modules."""

from __future__ import annotations

from datetime import datetime


def parse_dt(value: str | None) -> datetime | None:
    """Parse an ISO-format string to a datetime, or return None."""
    if value is None:
        return None
    return datetime.fromisoformat(value)


def fmt_dt(value: datetime | None) -> str | None:
    """Format a datetime as ISO string, or return None."""
    if value is None:
        return None
    return value.isoformat()
