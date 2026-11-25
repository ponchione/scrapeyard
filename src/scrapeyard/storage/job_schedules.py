"""Scheduled-job mutation and trigger-failure queries for jobs.db."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import cast

import aiosqlite

from scrapeyard.common.dt import fmt_dt
from scrapeyard.models.job import Job, JobMetadata, JobStatus
from scrapeyard.storage.job_queries import SCHEDULED_JOBS_QUERY
from scrapeyard.storage.job_rows import row_to_schedule_state
from scrapeyard.storage.types import (
    ScheduledJobMutationAction,
    ScheduledJobMutationOutcome,
)

_SCHEDULE_FAILURE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


async def update_scheduled_job(
    db: aiosqlite.Connection,
    job_id: str,
    *,
    project: str,
    name: str,
    config_yaml: str,
    config_hash: str,
    protected_config: str,
    schedule_cron: str,
    schedule_timezone: str,
    schedule_enabled: bool,
    updated_at: datetime,
    get_job_in_db: Callable[[aiosqlite.Connection, str], Awaitable[Job | None]],
    is_duplicate_error: Callable[[Exception], bool],
    duplicate_error_cls: type[Exception],
) -> ScheduledJobMutationOutcome:
    """Replace future scheduled config while excluding an accepted run."""

    previous = await get_job_in_db(db, job_id)
    if previous is None:
        return ScheduledJobMutationOutcome(ScheduledJobMutationAction.missing)
    if previous.schedule_cron is None:
        return ScheduledJobMutationOutcome(
            ScheduledJobMutationAction.not_scheduled,
            previous,
            previous,
        )
    if previous.project != project:
        return ScheduledJobMutationOutcome(
            ScheduledJobMutationAction.project_conflict,
            previous,
            previous,
        )
    if previous.status in {JobStatus.cancelled, JobStatus.deleting}:
        return ScheduledJobMutationOutcome(
            ScheduledJobMutationAction.lifecycle_conflict,
            previous,
            previous,
        )
    if previous.status is JobStatus.running or (
        previous.status is JobStatus.queued and previous.current_run_id is not None
    ):
        return ScheduledJobMutationOutcome(
            ScheduledJobMutationAction.active_conflict,
            previous,
            previous,
        )
    try:
        await db.execute(
            """UPDATE jobs
               SET project = ?, name = ?, config_yaml = ?, config_hash = ?,
                   schedule_cron = ?, schedule_timezone = ?,
                   schedule_enabled = ?, updated_at = ?
               WHERE job_id = ?""",
            (
                project,
                name,
                protected_config,
                config_hash,
                schedule_cron,
                schedule_timezone,
                int(schedule_enabled),
                fmt_dt(updated_at),
                job_id,
            ),
        )
    except aiosqlite.IntegrityError as exc:
        if is_duplicate_error(exc):
            raise duplicate_error_cls(project, name) from exc
        raise
    current = previous.model_copy(
        update={
            "project": project,
            "name": name,
            "config_yaml": config_yaml,
            "schedule_cron": schedule_cron,
            "schedule_timezone": schedule_timezone,
            "schedule_enabled": schedule_enabled,
            "updated_at": updated_at,
        }
    )
    return ScheduledJobMutationOutcome(
        ScheduledJobMutationAction.updated,
        previous,
        current,
    )


async def set_schedule_enabled(
    db: aiosqlite.Connection,
    job_id: str,
    *,
    enabled: bool,
    updated_at: datetime,
    get_metadata_in_db: Callable[[aiosqlite.Connection, str], Awaitable[JobMetadata | None]],
) -> ScheduledJobMutationOutcome:
    """Persist pause/resume state independently of current run execution."""

    previous = await get_metadata_in_db(db, job_id)
    if previous is None:
        return ScheduledJobMutationOutcome(ScheduledJobMutationAction.missing)
    if previous.schedule_cron is None:
        return ScheduledJobMutationOutcome(
            ScheduledJobMutationAction.not_scheduled,
            previous,
            previous,
        )
    if previous.status in {JobStatus.cancelled, JobStatus.deleting}:
        return ScheduledJobMutationOutcome(
            ScheduledJobMutationAction.lifecycle_conflict,
            previous,
            previous,
        )
    if previous.schedule_enabled is enabled:
        return ScheduledJobMutationOutcome(
            ScheduledJobMutationAction.unchanged,
            previous,
            previous,
        )
    await db.execute(
        """UPDATE jobs SET schedule_enabled = ?, updated_at = ?
           WHERE job_id = ?""",
        (int(enabled), fmt_dt(updated_at), job_id),
    )
    current = previous.model_copy(
        update={"schedule_enabled": enabled, "updated_at": updated_at}
    )
    return ScheduledJobMutationOutcome(
        ScheduledJobMutationAction.updated,
        previous,
        current,
    )


async def restore_scheduled_job(
    db: aiosqlite.Connection,
    expected: Job | JobMetadata,
    previous: Job | JobMetadata,
    *,
    expected_hash: str,
    previous_hash: str | None = None,
    previous_ciphertext: str | None = None,
) -> bool:
    """Restore one exact just-written snapshot after local scheduler failure."""

    if isinstance(previous, JobMetadata):
        cursor = await db.execute(
            """UPDATE jobs
               SET project = ?, name = ?, schedule_cron = ?,
                   schedule_timezone = ?, schedule_enabled = ?, updated_at = ?
               WHERE job_id = ?
                 AND project = ? AND name = ? AND config_hash = ?
                 AND schedule_cron IS ? AND schedule_timezone = ?
                 AND schedule_enabled = ? AND updated_at IS ?
                 AND status = ? AND current_run_id IS ?""",
            (
                previous.project,
                previous.name,
                previous.schedule_cron,
                previous.schedule_timezone,
                int(previous.schedule_enabled),
                fmt_dt(previous.updated_at),
                expected.job_id,
                expected.project,
                expected.name,
                expected_hash,
                expected.schedule_cron,
                expected.schedule_timezone,
                int(expected.schedule_enabled),
                fmt_dt(expected.updated_at),
                expected.status.value,
                expected.current_run_id,
            ),
        )
        return cursor.rowcount == 1

    cursor = await db.execute(
        """UPDATE jobs
           SET project = ?, name = ?, config_yaml = ?, config_hash = ?, schedule_cron = ?,
               schedule_timezone = ?, schedule_enabled = ?, updated_at = ?
           WHERE job_id = ?
             AND project = ? AND name = ? AND config_hash = ?
             AND schedule_cron IS ? AND schedule_timezone = ?
             AND schedule_enabled = ? AND updated_at IS ?
             AND status = ? AND current_run_id IS ?""",
        (
            previous.project,
            previous.name,
            previous_ciphertext,
            previous_hash,
            previous.schedule_cron,
            previous.schedule_timezone,
            int(previous.schedule_enabled),
            fmt_dt(previous.updated_at),
            expected.job_id,
            expected.project,
            expected.name,
            expected_hash,
            expected.schedule_cron,
            expected.schedule_timezone,
            int(expected.schedule_enabled),
            fmt_dt(expected.updated_at),
            expected.status.value,
            expected.current_run_id,
        ),
    )
    return cursor.rowcount == 1


async def rollback_scheduled_job_creation(
    db: aiosqlite.Connection,
    job_id: str,
) -> bool:
    """Remove a scheduled row only before any run or delivery exists."""

    cursor = await db.execute(
        """DELETE FROM jobs
           WHERE job_id = ?
             AND schedule_cron IS NOT NULL
             AND status = 'queued'
             AND current_run_id IS NULL
             AND NOT EXISTS (
                 SELECT 1 FROM job_runs WHERE job_runs.job_id = jobs.job_id
             )
             AND NOT EXISTS (
                 SELECT 1 FROM webhook_deliveries
                 WHERE webhook_deliveries.job_id = jobs.job_id
             )""",
        (job_id,),
    )
    return cursor.rowcount == 1


async def record_scheduled_trigger_failure(
    db: aiosqlite.Connection,
    job_id: str,
    run_id: str,
    *,
    failed_at: datetime,
    failure_code: str,
) -> bool:
    """Persist one sanitized missed fire and the schedule's degraded state."""

    if _SCHEDULE_FAILURE_CODE_RE.fullmatch(failure_code) is None:
        raise ValueError("Scheduled failure code must be a bounded safe identifier")
    failed_text = fmt_dt(failed_at)
    cursor = await db.execute(
        """SELECT config_hash, status, current_run_id
           FROM jobs
           WHERE job_id = ?
             AND schedule_cron IS NOT NULL
             AND schedule_enabled = 1
             AND status NOT IN ('cancelled', 'deleting')""",
        (job_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        await db.rollback()
        return False
    cursor = await db.execute(
        """INSERT OR IGNORE INTO job_runs
               (run_id, job_id, status, trigger, config_hash, config_yaml,
                started_at, heartbeat_at, completed_at, record_count,
                error_count, webhook_reconciled_at, failure_code)
               VALUES (?, ?, 'failed', 'scheduled', ?, NULL, ?, ?, ?, 0, 1, ?, ?)""",
        (
            run_id,
            job_id,
            cast(str, row["config_hash"]),
            failed_text,
            failed_text,
            failed_text,
            failed_text,
            failure_code,
        ),
    )
    if cursor.rowcount == 0:
        existing = await (
            await db.execute(
                "SELECT status FROM job_runs WHERE job_id = ? AND run_id = ?",
                (job_id, run_id),
            )
        ).fetchone()
        if existing is None or existing["status"] != JobStatus.failed.value:
            await db.rollback()
            return False

    parent_can_converge = (
        row["status"]
        not in {
            JobStatus.queued.value,
            JobStatus.running.value,
            JobStatus.cancelled.value,
            JobStatus.deleting.value,
        }
        or (
            row["status"] == JobStatus.queued.value
            and row["current_run_id"] in {None, run_id}
        )
    )
    await db.execute(
        """UPDATE jobs
           SET schedule_failure_at = ?,
               schedule_failure_code = ?,
               schedule_consecutive_failures = schedule_consecutive_failures + 1,
               status = CASE WHEN ? THEN 'failed' ELSE status END,
               current_run_id = CASE WHEN ? THEN ? ELSE current_run_id END,
               current_trigger = CASE WHEN ? THEN 'scheduled' ELSE current_trigger END,
               updated_at = CASE WHEN ? THEN ? ELSE updated_at END
           WHERE job_id = ?""",
        (
            failed_text,
            failure_code,
            int(parent_can_converge),
            int(parent_can_converge),
            run_id,
            int(parent_can_converge),
            int(parent_can_converge),
            failed_text,
            job_id,
        ),
    )
    return True


async def clear_scheduled_trigger_failure(
    db: aiosqlite.Connection,
    job_id: str,
) -> bool:
    """Clear degraded schedule state after a fire is accepted by Redis."""

    cursor = await db.execute(
        """UPDATE jobs
           SET schedule_failure_at = NULL,
               schedule_failure_code = NULL,
               schedule_consecutive_failures = 0
           WHERE job_id = ?
             AND schedule_cron IS NOT NULL
             AND schedule_failure_at IS NOT NULL""",
        (job_id,),
    )
    return cursor.rowcount == 1


async def list_scheduled_trigger_failures(
    db: aiosqlite.Connection,
) -> list[tuple[str, str]]:
    """Return unresolved failures for enabled, locally runnable schedules."""

    cursor = await db.execute(
        """SELECT job_id, schedule_failure_code
           FROM jobs
           WHERE schedule_cron IS NOT NULL
             AND schedule_enabled = 1
             AND status NOT IN ('cancelled', 'deleting')
             AND schedule_failure_at IS NOT NULL
             AND schedule_failure_code IS NOT NULL
           ORDER BY schedule_failure_at, job_id"""
    )
    return [
        (str(row["job_id"]), str(row["schedule_failure_code"]))
        for row in await cursor.fetchall()
    ]


async def list_scheduled_jobs(
    db: aiosqlite.Connection,
) -> list[tuple[str, str, str, bool]]:
    """Return ID, cron, timezone, and enabled state for scheduled jobs."""

    cursor = await db.execute(SCHEDULED_JOBS_QUERY)
    rows = cast(list[Mapping[str, object]], await cursor.fetchall())
    return [row_to_schedule_state(row) for row in rows]
