"""Webhook payload construction and firing logic."""

from __future__ import annotations

from scrapeyard.config.schema import WebhookConfig
from scrapeyard.models.job import JobStatus


def should_fire(config: WebhookConfig, status: JobStatus) -> bool:
    """Return True if the webhook should fire for the given job status."""
    return status.value in {s.value for s in config.on}


def build_webhook_payload(
    *,
    job_id: str,
    project: str,
    name: str,
    status: JobStatus,
    run_id: str,
    result_path: str,
    result_count: int | None,
    error_count: int,
    started_at: str,
    completed_at: str,
) -> dict:
    """Construct the webhook POST body from job and run metadata."""
    return {
        "event": f"job.{status.value}",
        "job_id": job_id,
        "project": project,
        "name": name,
        "status": status.value,
        "run_id": run_id,
        "result_path": result_path,
        "results_url": f"/results/{job_id}?run_id={run_id}",
        "result_count": result_count,
        "error_count": error_count,
        "started_at": started_at,
        "completed_at": completed_at,
    }
