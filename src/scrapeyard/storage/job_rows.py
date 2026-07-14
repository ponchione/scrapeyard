"""Row-mapping helpers for SQLite job storage."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import cast

from scrapeyard.common.dt import parse_dt
from scrapeyard.models.job import Job, JobRun, JobStatus
from scrapeyard.storage.secret_envelope import reveal_text


RowMapping = Mapping[str, object]


def _optional_row_value(row: RowMapping, key: str) -> object | None:
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def row_to_job(row: RowMapping) -> Job:
    created_at = parse_dt(cast(str | None, row["created_at"]))
    if created_at is None:
        raise ValueError("Job row is missing created_at")
    job_id = cast(str, row["job_id"])
    return Job(
        job_id=job_id,
        project=cast(str, row["project"]),
        name=cast(str, row["name"]),
        status=JobStatus(cast(str, row["status"])),
        config_yaml=reveal_text(
            cast(str, row["config_yaml"]),
            purpose=f"jobs.config_yaml:{job_id}",
        ),
        created_at=created_at,
        updated_at=parse_dt(cast(str | None, row["updated_at"])),
        schedule_cron=cast(str | None, row["schedule_cron"]),
        schedule_timezone=cast(
            str,
            _optional_row_value(row, "schedule_timezone") or "UTC",
        ),
        schedule_enabled=bool(row["schedule_enabled"]),
        current_run_id=cast(str | None, row["current_run_id"]),
        current_trigger=cast(
            str | None,
            _optional_row_value(row, "current_trigger"),
        ),
        deletion_requested_at=parse_dt(
            cast(str | None, _optional_row_value(row, "deletion_requested_at"))
        ),
        delete_results_on_delete=(
            None
            if _optional_row_value(row, "delete_results_on_delete") is None
            else bool(_optional_row_value(row, "delete_results_on_delete"))
        ),
        schedule_failure_at=parse_dt(
            cast(str | None, _optional_row_value(row, "schedule_failure_at"))
        ),
        schedule_failure_code=cast(
            str | None,
            _optional_row_value(row, "schedule_failure_code"),
        ),
        schedule_consecutive_failures=cast(
            int,
            _optional_row_value(row, "schedule_consecutive_failures") or 0,
        ),
    )


def row_to_job_run(row: RowMapping) -> JobRun:
    started_at = parse_dt(cast(str | None, row["started_at"]))
    if started_at is None:
        raise ValueError("Job run row is missing started_at")
    heartbeat_at = parse_dt(cast(str | None, row["heartbeat_at"]))
    if heartbeat_at is None:
        raise ValueError("Job run row is missing heartbeat_at")
    return JobRun(
        run_id=cast(str, row["run_id"]),
        job_id=cast(str, row["job_id"]),
        status=JobStatus(cast(str, row["status"])),
        trigger=cast(str, row["trigger"]),
        config_hash=cast(str, row["config_hash"]),
        started_at=started_at,
        heartbeat_at=heartbeat_at,
        completed_at=parse_dt(cast(str | None, row["completed_at"])),
        record_count=cast(int | None, row["record_count"]),
        error_count=cast(int, row["error_count"]),
        failure_code=cast(str | None, _optional_row_value(row, "failure_code")),
    )


def row_to_job_with_stats(
    row: RowMapping,
) -> tuple[Job, int, datetime | None]:
    job = row_to_job(row)
    run_count = cast(int, row["run_count"])
    last_run_at = parse_dt(cast(str | None, row["last_run_at"]))
    return job, run_count, last_run_at


def row_to_project_summary(row: RowMapping) -> tuple[str, str, int]:
    return cast(str, row["project"]), cast(str, row["status"]), cast(int, row["count"])


def row_to_schedule_state(row: RowMapping) -> tuple[str, str, str, bool]:
    return (
        cast(str, row["job_id"]),
        cast(str, row["schedule_cron"]),
        cast(str, row["schedule_timezone"]),
        bool(row["schedule_enabled"]),
    )
