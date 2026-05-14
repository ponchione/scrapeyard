"""Shared identifier helpers."""

from __future__ import annotations

import uuid

from scrapeyard.common.time import utc_now


def generate_run_id() -> str:
    """Return a sortable run identifier."""
    now = utc_now()
    short_uuid = uuid.uuid4().hex[:16]
    return f"{now.strftime('%Y%m%d-%H%M%S')}-{short_uuid}"
