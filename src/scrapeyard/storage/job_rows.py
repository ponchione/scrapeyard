"""Row-mapping helpers for SQLite job storage."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import cast

from scrapeyard.common.dt import parse_dt
from scrapeyard.models.job import Job, JobRun, JobStatus


JOB_COLUMN_COUNT = 10


def row_to_job(row: Sequence[object]) -> Job:
    created_at = parse_dt(cast(str | None, row[5]))
    if created_at is None:
        raise ValueError("Job row is missing created_at")
    return Job(
        job_id=cast(str, row[0]),
        project=cast(str, row[1]),
        name=cast(str, row[2]),
        status=JobStatus(cast(str, row[3])),
        config_yaml=cast(str, row[4]),
        created_at=created_at,
        updated_at=parse_dt(cast(str | None, row[6])),
        schedule_cron=cast(str | None, row[7]),
        schedule_enabled=bool(row[8]),
        current_run_id=cast(str | None, row[9]),
    )


def row_to_job_run(row: Sequence[object]) -> JobRun:
    started_at = parse_dt(cast(str | None, row[5]))
    if started_at is None:
        raise ValueError("Job run row is missing started_at")
    return JobRun(
        run_id=cast(str, row[0]),
        job_id=cast(str, row[1]),
        status=JobStatus(cast(str, row[2])),
        trigger=cast(str, row[3]),
        config_hash=cast(str, row[4]),
        started_at=started_at,
        completed_at=parse_dt(cast(str | None, row[6])),
        record_count=cast(int | None, row[7]),
        error_count=cast(int, row[8]),
    )


def row_to_job_with_stats(
    row: Sequence[object],
) -> tuple[Job, int, datetime | None]:
    job = row_to_job(row[:JOB_COLUMN_COUNT])
    run_count = cast(int, row[JOB_COLUMN_COUNT])
    last_run_at = parse_dt(cast(str | None, row[JOB_COLUMN_COUNT + 1]))
    return job, run_count, last_run_at


def row_to_project_summary(row: Sequence[object]) -> tuple[str, str, int]:
    return cast(str, row[0]), cast(str, row[1]), cast(int, row[2])


def row_to_schedule_state(row: Sequence[object]) -> tuple[str, str, bool]:
    return cast(str, row[0]), cast(str, row[1]), bool(row[2])
