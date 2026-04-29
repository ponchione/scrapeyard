"""SQLite-backed implementation of the JobStore protocol."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

import aiosqlite

from scrapeyard.common.dt import fmt_dt
from scrapeyard.common.time import utc_now
from scrapeyard.models.job import Job, JobRun
from scrapeyard.storage.database import get_db
from scrapeyard.storage.job_queries import (
    PROJECT_SUMMARY_QUERY,
    SCHEDULED_JOBS_QUERY,
    build_list_jobs_with_stats_query,
)
from scrapeyard.storage.job_rows import (
    row_to_job,
    row_to_job_run,
    row_to_job_with_stats,
    row_to_project_summary,
    row_to_schedule_state,
)
from scrapeyard.storage.job_sql import JOB_COLUMNS, JOB_RUN_COLUMNS, select_columns


class DuplicateJobError(Exception):
    """Raised when a job name already exists within a project namespace."""

    def __init__(self, project: str, name: str) -> None:
        self.project = project
        self.name = name
        super().__init__(f"Job {name!r} already exists in project {project!r}")


def is_duplicate_job_integrity_error(error: str | Exception) -> bool:
    message = error if isinstance(error, str) else str(error)
    return (
        "UNIQUE constraint failed: jobs.project, jobs.name" in message
        or "jobs.project, jobs.name" in message
    )


class SQLiteJobStore:
    """SQLite implementation of :class:`~scrapeyard.storage.protocols.JobStore`."""

    async def _execute_write(
        self,
        sql: str,
        params: Sequence[object] = (),
    ) -> aiosqlite.Cursor:
        async with get_db("jobs.db") as db:
            cursor = await db.execute(sql, params)
            await db.commit()
            return cursor

    @staticmethod
    def _raise_if_missing_job(cursor: aiosqlite.Cursor, job_id: str) -> None:
        if cursor.rowcount == 0:
            raise KeyError(f"Job not found: {job_id!r}")

    async def _execute_job_update(
        self,
        sql: str,
        params: Sequence[object],
        job_id: str,
    ) -> None:
        cursor = await self._execute_write(sql, params)
        self._raise_if_missing_job(cursor, job_id)

    async def save_job(self, job: Job) -> str:
        async with get_db("jobs.db") as db:
            try:
                await db.execute(
                    """INSERT INTO jobs (job_id, project, name, status,
                       config_yaml, created_at, updated_at, schedule_cron,
                       schedule_enabled, current_run_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        job.job_id,
                        job.project,
                        job.name,
                        job.status.value,
                        job.config_yaml,
                        fmt_dt(job.created_at),
                        fmt_dt(job.updated_at),
                        job.schedule_cron,
                        int(job.schedule_enabled),
                        job.current_run_id,
                    ),
                )
            except aiosqlite.IntegrityError as exc:
                if is_duplicate_job_integrity_error(exc):
                    raise DuplicateJobError(job.project, job.name) from exc
                raise
            await db.commit()
        return job.job_id

    async def update_job(self, job: Job) -> None:
        await self._execute_job_update(
            """UPDATE jobs SET project=?, name=?, status=?,
               config_yaml=?, created_at=?, updated_at=?,
               schedule_cron=?, schedule_enabled=?,
               current_run_id=?
               WHERE job_id=?""",
            (
                job.project,
                job.name,
                job.status.value,
                job.config_yaml,
                fmt_dt(job.created_at),
                fmt_dt(job.updated_at),
                job.schedule_cron,
                int(job.schedule_enabled),
                job.current_run_id,
                job.job_id,
            ),
            job.job_id,
        )

    async def update_job_status(self, job: Job) -> None:
        await self._execute_job_update(
            """UPDATE jobs SET status=?, updated_at=?,
               current_run_id=?
               WHERE job_id=?""",
            (
                job.status.value,
                fmt_dt(job.updated_at),
                job.current_run_id,
                job.job_id,
            ),
            job.job_id,
        )

    async def update_job_schedule_state(self, job: Job) -> None:
        await self._execute_job_update(
            """UPDATE jobs SET schedule_cron=?, schedule_enabled=?,
               updated_at=?
               WHERE job_id=?""",
            (
                job.schedule_cron,
                int(job.schedule_enabled),
                fmt_dt(job.updated_at),
                job.job_id,
            ),
            job.job_id,
        )

    async def get_job(self, job_id: str) -> Job:
        async with get_db("jobs.db") as db:
            cursor = await db.execute(
                f"SELECT {select_columns(JOB_COLUMNS)} FROM jobs WHERE job_id=?",
                (job_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            raise KeyError(f"Job not found: {job_id!r}")
        return row_to_job(row)

    async def list_jobs(self, project: str | None = None) -> list[Job]:
        async with get_db("jobs.db") as db:
            if project is not None:
                cursor = await db.execute(
                    f"SELECT {select_columns(JOB_COLUMNS)} FROM jobs WHERE project=?",
                    (project,),
                )
            else:
                cursor = await db.execute(
                    f"SELECT {select_columns(JOB_COLUMNS)} FROM jobs"
                )
            rows = await cursor.fetchall()
        return [row_to_job(row) for row in rows]

    async def get_job_runs(self, job_id: str, limit: int = 10) -> list[JobRun]:
        """Return the last N runs for a job, newest first."""
        async with get_db("jobs.db") as db:
            cursor = await db.execute(
                f"SELECT {select_columns(JOB_RUN_COLUMNS)} FROM job_runs WHERE job_id = ? "
                "ORDER BY started_at DESC LIMIT ?",
                (job_id, limit),
            )
            return [row_to_job_run(row) for row in await cursor.fetchall()]

    async def get_job_run_stats(
        self, job_id: str,
    ) -> tuple[int, datetime | None]:
        """Return (run_count, last_run_at) derived from job_runs."""
        async with get_db("jobs.db") as db:
            cursor = await db.execute(
                "SELECT COUNT(*) AS run_count, MAX(started_at) AS last_run_at "
                "FROM job_runs WHERE job_id = ?",
                (job_id,),
            )
            row = await cursor.fetchone()
            count = row["run_count"] if row else 0
            last = (
                datetime.fromisoformat(row["last_run_at"]) if row and row["last_run_at"] else None
            )
            return count, last

    async def list_jobs_with_stats(
        self,
        project: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[tuple[Job, int, datetime | None]]:
        """List jobs with derived run_count and last_run_at, newest activity first."""
        sql, params = build_list_jobs_with_stats_query(project, limit, offset)
        async with get_db("jobs.db") as db:
            cursor = await db.execute(sql, params)
            rows = await cursor.fetchall()
        return [row_to_job_with_stats(row) for row in rows]

    async def summary_by_project(self) -> list[tuple[str, str, int]]:
        """Return grouped job counts by project and status."""
        async with get_db("jobs.db") as db:
            cursor = await db.execute(PROJECT_SUMMARY_QUERY)
            rows = await cursor.fetchall()
        return [row_to_project_summary(row) for row in rows]

    async def create_run(
        self,
        run_id: str,
        job_id: str,
        trigger: str,
        config_hash: str,
        started_at: datetime,
    ) -> None:
        """Insert a new job_runs row with status='running'."""
        await self._execute_write(
            """INSERT INTO job_runs
               (run_id, job_id, status, trigger,
                config_hash, started_at)
               VALUES (?, ?, 'running', ?, ?, ?)""",
            (
                run_id, job_id, trigger,
                config_hash,
                fmt_dt(started_at),
            ),
        )

    async def finalize_run(
        self,
        run_id: str,
        status: str,
        record_count: int,
        error_count: int,
    ) -> None:
        """Update a job_runs row with final status, counts, and completed_at."""
        await self._execute_write(
            """UPDATE job_runs
               SET status = ?, completed_at = ?,
                   record_count = ?, error_count = ?
               WHERE run_id = ?""",
            (
                status,
                fmt_dt(utc_now()),
                record_count,
                error_count,
                run_id,
            ),
        )

    async def fail_run(self, run_id: str) -> None:
        """Mark a running run as failed (crash recovery)."""
        await self._execute_write(
            """UPDATE job_runs
               SET status = 'failed',
                   completed_at = ?
               WHERE run_id = ?
                 AND status = 'running'""",
            (
                fmt_dt(utc_now()),
                run_id,
            ),
        )

    async def recover_stale_running_jobs(
        self,
        cutoff: datetime,
        recovered_at: datetime,
    ) -> int:
        """Fail stale running runs and matching running jobs after a crash.

        A process crash can leave either:
        - a ``job_runs`` row stuck in ``running``; or
        - a ``jobs`` row stuck in ``running`` before its run row was created.

        This startup recovery pass marks stale run rows failed, then transitions
        only matching/stale ``jobs`` rows to ``failed``. Jobs with a fresh active
        run are left alone.
        """
        cutoff_text = fmt_dt(cutoff)
        recovered_text = fmt_dt(recovered_at)
        async with get_db("jobs.db") as db:
            cursor = await db.execute(
                """SELECT run_id
                   FROM job_runs
                   WHERE status = 'running'
                     AND started_at <= ?""",
                (cutoff_text,),
            )
            stale_run_ids = [row["run_id"] for row in await cursor.fetchall()]

            recovered_jobs = 0
            if stale_run_ids:
                placeholders = ",".join("?" for _ in stale_run_ids)
                await db.execute(
                    f"""UPDATE job_runs
                       SET status = 'failed', completed_at = ?
                       WHERE status = 'running'
                         AND run_id IN ({placeholders})""",
                    (recovered_text, *stale_run_ids),
                )
                cursor = await db.execute(
                    f"""UPDATE jobs
                       SET status = 'failed', updated_at = ?
                       WHERE status = 'running'
                         AND current_run_id IN ({placeholders})""",
                    (recovered_text, *stale_run_ids),
                )
                recovered_jobs += max(cursor.rowcount, 0)

            cursor = await db.execute(
                """UPDATE jobs
                   SET status = 'failed', updated_at = ?
                   WHERE status = 'running'
                     AND (updated_at IS NULL OR updated_at <= ?)
                     AND (
                         current_run_id IS NULL
                         OR NOT EXISTS (
                             SELECT 1
                             FROM job_runs
                             WHERE job_runs.run_id = jobs.current_run_id
                               AND job_runs.status = 'running'
                         )
                     )""",
                (recovered_text, cutoff_text),
            )
            recovered_jobs += max(cursor.rowcount, 0)
            await db.commit()
            return recovered_jobs

    async def list_scheduled_jobs(self) -> list[tuple[str, str, bool]]:
        """Return (job_id, schedule_cron, schedule_enabled) for all scheduled jobs."""
        async with get_db("jobs.db") as db:
            cursor = await db.execute(SCHEDULED_JOBS_QUERY)
            rows = await cursor.fetchall()
        return [row_to_schedule_state(row) for row in rows]

    async def delete_job(self, job_id: str) -> None:
        async with get_db("jobs.db") as db:
            await db.execute("DELETE FROM job_runs WHERE job_id=?", (job_id,))
            await db.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))
            await db.commit()
