"""Helpers for explicit job-state transitions."""

from __future__ import annotations

from datetime import datetime

from scrapeyard.models.job import Job, JobStatus


def build_running_job(job: Job, *, started_at: datetime) -> Job:
    """Return a copy of *job* transitioned to running."""
    return job.model_copy(update={
        "status": JobStatus.running,
        "updated_at": started_at,
    })


def build_completed_job(
    job: Job,
    *,
    final_status: JobStatus,
    completed_at: datetime,
    run_id: str | None,
) -> Job:
    """Return a copy of *job* transitioned to a terminal state."""
    return job.model_copy(update={
        "status": final_status,
        "updated_at": completed_at,
        "current_run_id": run_id,
    })


def build_failed_job(job: Job, *, failed_at: datetime) -> Job:
    """Return a copy of *job* transitioned to failed."""
    return job.model_copy(update={
        "status": JobStatus.failed,
        "updated_at": failed_at,
    })
