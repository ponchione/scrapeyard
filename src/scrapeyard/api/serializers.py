"""Serialization helpers for API responses."""

from __future__ import annotations

from datetime import datetime
import hashlib
from typing import Any

from scrapeyard.engine.url_guard import (
    redact_sensitive_config_text,
    redact_userinfo_in_text,
    redact_userinfo_in_url,
)
from scrapeyard.models.job import ErrorRecord, Job, JobRun
from scrapeyard.api.response_models import APICompatibility


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _contract_datetime(value: Any) -> Any:
    if isinstance(value, str) and value.endswith("+00:00"):
        return value[:-6] + "Z"
    return value


def serialize_job_run(run: JobRun) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "status": run.status.value,
        "trigger": run.trigger,
        "config_hash": run.config_hash,
        "started_at": run.started_at.isoformat(),
        "heartbeat_at": run.heartbeat_at.isoformat(),
        "completed_at": _isoformat(run.completed_at),
        "record_count": run.record_count,
        "error_count": run.error_count,
        "failure_code": run.failure_code,
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
        "schedule_timezone": job.schedule_timezone,
        "schedule_enabled": job.schedule_enabled,
        "schedule_failure_at": _isoformat(job.schedule_failure_at),
        "schedule_failure_code": job.schedule_failure_code,
        "schedule_consecutive_failures": job.schedule_consecutive_failures,
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
        "schedule_timezone": job.schedule_timezone,
        "schedule_enabled": job.schedule_enabled,
        "schedule_failure_at": _isoformat(job.schedule_failure_at),
        "schedule_failure_code": job.schedule_failure_code,
        "schedule_consecutive_failures": job.schedule_consecutive_failures,
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
        "target_url": redact_userinfo_in_url(error.target_url),
        "attempt": error.attempt,
        "timestamp": error.timestamp.isoformat(),
        "error_type": error.error_type.value,
        "http_status": error.http_status,
        "fetcher_used": error.fetcher_used,
        "error_message": (
            redact_userinfo_in_text(error.error_message)
            if error.error_message is not None
            else None
        ),
        "selectors_matched": error.selectors_matched,
        "budget": error.budget.model_dump(mode="json") if error.budget is not None else None,
        "action_taken": error.action_taken.value,
        "resolved": error.resolved,
    }


def serialize_job_created(job: Job) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "project": job.project,
        "name": job.name,
        "schedule": job.schedule_cron,
        "schedule_timezone": job.schedule_timezone,
    }


def serialize_schedule_state(job: Job) -> dict[str, Any]:
    """Serialize the persisted future-run configuration identity."""

    return {
        "job_id": job.job_id,
        "project": job.project,
        "name": job.name,
        "status": job.status.value,
        "schedule_cron": job.schedule_cron,
        "schedule_timezone": job.schedule_timezone,
        "schedule_enabled": job.schedule_enabled,
        "config_hash": hashlib.sha256(job.config_yaml.encode("utf-8")).hexdigest(),
        "updated_at": _isoformat(job.updated_at),
    }


def serialize_scrape_queued(
    job_id: str,
    *,
    run_id: str,
    status: str,
    poll_url: str,
) -> dict[str, str]:
    return {
        "job_id": job_id,
        "run_id": run_id,
        "status": status,
        "poll_url": poll_url,
    }


def serialize_result_response(
    job_id: str,
    *,
    run_id: str,
    status: str,
    artifact: Any,
    compatibility: APICompatibility = APICompatibility.v1,
) -> dict[str, Any]:
    if compatibility is APICompatibility.legacy_v0:
        return {
            "job_id": job_id,
            "run_id": run_id,
            "status": status,
            "results": artifact,
        }
    if (
        isinstance(artifact, dict)
        and artifact.get("job_id") == job_id
        and "results" in artifact
        and isinstance(artifact.get("targets"), list)
    ):
        stored_targets = artifact.get("targets") or []
        targets = stored_targets
        if any(
            isinstance(target, dict) and "observed_count" not in target
            for target in stored_targets
        ):
            targets = list(stored_targets)
            for index, target in enumerate(targets):
                if isinstance(target, dict) and "observed_count" not in target:
                    normalized = dict(target)
                    normalized["observed_count"] = normalized.get("count", 0)
                    targets[index] = normalized
        return {
            "job_id": job_id,
            "run_id": run_id,
            "status": status,
            "completed_at": _contract_datetime(artifact.get("completed_at")),
            "errors": artifact.get("errors") or [],
            "targets": targets,
            "budget_error": artifact.get("budget_error"),
            "results": artifact["results"],
        }
    return {
        "job_id": job_id,
        "run_id": run_id,
        "status": status,
        "completed_at": None,
        "errors": [],
        "targets": [],
        "budget_error": None,
        "results": artifact,
    }
