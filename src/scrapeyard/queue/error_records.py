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


def build_target_result_error_records(
    *,
    job_id: str,
    run_id: str | None,
    project: str,
    target_url: str,
    attempt: int,
    fetcher_used: str,
    action: ActionTaken,
    result: TargetResult,
    default_error_type: ErrorType = ErrorType.http_error,
    combine_errors: bool = False,
) -> list[ErrorRecord]:
    """Build one or more error records from a failed target result."""
    if combine_errors and result.errors:
        combined = result.error_detail or "; ".join(result.errors)
        messages = [combined for _ in result.errors]
    else:
        messages = result.errors or [result.error_detail or "unknown scrape failure"]

    return [
        build_error_record(
            job_id,
            run_id or "",
            project,
            target_url,
            attempt,
            result.error_type or default_error_type,
            result.http_status,
            fetcher_used,
            action,
            error_message=message,
        )
        for message in messages
    ]
