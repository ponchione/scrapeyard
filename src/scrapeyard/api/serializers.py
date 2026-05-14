"""Serialization helpers for API responses."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from scrapeyard.engine.url_guard import redact_sensitive_config_text
from scrapeyard.models.job import ErrorRecord, Job, JobRun


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def serialize_job_run(run: JobRun) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "status": run.status.value,
        "trigger": run.trigger,
        "config_hash": run.config_hash,
        "started_at": run.started_at.isoformat(),
        "completed_at": _isoformat(run.completed_at),
        "record_count": run.record_count,
        "error_count": run.error_count,
    }


def serialize_job_summary(
    job: Job,
    *,
    run_count: int,
    last_run_at: datetime | None,
) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "project": job.project,
        "name": job.name,
        "status": job.status.value,
        "created_at": job.created_at.isoformat(),
        "updated_at": _isoformat(job.updated_at),
        "schedule_cron": job.schedule_cron,
        "schedule_enabled": job.schedule_enabled,
        "run_count": run_count,
        "last_run_at": _isoformat(last_run_at),
    }


def serialize_job_detail(
    job: Job,
    *,
    runs: list[JobRun],
    run_count: int,
    last_run_at: datetime | None,
    next_run_at: datetime | None,
) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "project": job.project,
        "name": job.name,
        "status": job.status.value,
        "config_yaml": redact_sensitive_config_text(job.config_yaml),
        "created_at": job.created_at.isoformat(),
        "updated_at": _isoformat(job.updated_at),
        "schedule_cron": job.schedule_cron,
        "schedule_enabled": job.schedule_enabled,
        "next_run_at": _isoformat(next_run_at),
        "run_count": run_count,
        "last_run_at": _isoformat(last_run_at),
        "runs": [serialize_job_run(run) for run in runs],
    }


def serialize_error_record(error: ErrorRecord) -> dict[str, Any]:
    return {
        "job_id": error.job_id,
        "run_id": error.run_id,
        "project": error.project,
        "target_url": error.target_url,
        "attempt": error.attempt,
        "timestamp": error.timestamp.isoformat(),
        "error_type": error.error_type.value,
        "http_status": error.http_status,
        "fetcher_used": error.fetcher_used,
        "error_message": error.error_message,
        "selectors_matched": error.selectors_matched,
        "action_taken": error.action_taken.value,
        "resolved": error.resolved,
    }


def serialize_job_created(job: Job) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "project": job.project,
        "name": job.name,
        "schedule": job.schedule_cron,
    }


def serialize_scrape_queued(job_id: str, *, status: str, poll_url: str) -> dict[str, str]:
    return {
        "job_id": job_id,
        "status": status,
        "poll_url": poll_url,
    }


def serialize_scrape_result(job_id: str, *, status: str, results: Any) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "status": status,
        "results": results,
    }


def serialize_results_payload(job_id: str, *, run_id: str, status: str, results: Any) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "run_id": run_id,
        "status": status,
        "results": results,
    }
