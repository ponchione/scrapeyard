"""Shared identifier helpers."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone


def generate_run_id() -> str:
    """Return a sortable run identifier."""
    now = datetime.now(timezone.utc)
    short_uuid = uuid.uuid4().hex[:8]
    return f"{now.strftime('%Y%m%d-%H%M%S')}-{short_uuid}"
