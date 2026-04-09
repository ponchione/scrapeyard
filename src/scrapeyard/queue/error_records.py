"""Shared helpers for worker-side error classification and record creation."""

from __future__ import annotations

from scrapeyard.engine.scraper import TargetResult
from scrapeyard.models.job import ActionTaken, ErrorRecord, ErrorType


def validation_error_type(result: TargetResult) -> ErrorType:
    if result.error_type is not None:
        return result.error_type
    if result.debug and isinstance(result.debug, dict):
        classification = result.debug.get("classification")
        if classification is not None:
            try:
                return ErrorType(classification)
            except ValueError:
                pass
    return ErrorType.content_empty


def build_error_record(
    job_id: str,
    run_id: str,
    project: str,
    url: str,
    attempt: int,
    error_type: ErrorType,
    http_status: int | None,
    fetcher_used: str,
    action: ActionTaken,
    error_message: str | None = None,
) -> ErrorRecord:
    """Build a structured error record for deferred persistence."""
    return ErrorRecord(
        job_id=job_id,
        run_id=run_id,
        project=project,
        target_url=url,
        attempt=attempt,
        error_type=error_type,
        http_status=http_status,
        fetcher_used=fetcher_used,
        error_message=error_message,
        action_taken=action,
    )
