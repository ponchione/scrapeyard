"""Query-string parsing helpers for API routes."""

from __future__ import annotations

from datetime import datetime

from scrapeyard.api.response_utils import raise_json_error
from scrapeyard.models.job import ErrorFilters, ErrorType


def parse_error_filters(
    *,
    project: str | None,
    job_id: str | None,
    since: str | None,
    error_type: str | None,
) -> ErrorFilters:
    try:
        since_dt = datetime.fromisoformat(since) if since else None
    except ValueError:
        raise_json_error(400, f"Invalid 'since' format: {since!r}")
    try:
        error_type_enum = ErrorType(error_type) if error_type else None
    except ValueError:
        raise_json_error(400, f"Invalid 'error_type': {error_type!r}")
    return ErrorFilters(
        project=project,
        job_id=job_id,
        since=since_dt,
        error_type=error_type_enum,
    )
