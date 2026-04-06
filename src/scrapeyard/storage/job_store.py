"""SQLite-backed implementation of the JobStore protocol."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import cast

import aiosqlite

from scrapeyard.common.dt import fmt_dt, parse_dt
from scrapeyard.models.job import Job, JobRun, JobStatus
from scrapeyard.storage.database import get_db


_JOB_COLUMNS = (
    "job_id, project, name, status, config_yaml, "
    "created_at, updated_at, schedule_cron, schedule_enabled, "
    "current_run_id"
)

_JOB_RUN_COLUMNS = (
    "run_id, job_id, status, trigger, config_hash, "
    "started_at, completed_at, record_count, error_count"
)


class DuplicateJobError(Exception):
    """Raised when a job name already exists within a project namespace."""

    def __init__(self, project: str, name: str) -> None:
        self.project = project
        self.name = name
        super().__init__(f"Job {name!r} already exists in project {project!r}")


def _row_to_job(row: Sequence[object]) -> Job:
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


def _row_to_job_run(row: aiosqlite.Row) -> JobRun:
    return JobRun(
        run_id=row[0], job_id=row[1], status=row[2], trigger=row[3],
        config_hash=row[4], started_at=parse_dt(row[5]),  # type: ignore[arg-type]
        completed_at=parse_dt(row[6]), record_count=row[7], error_count=row[8],
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
                if "jobs.project, jobs.name" in str(exc) or "UNIQUE constraint failed: jobs.project, jobs.name" in str(exc):
                    raise DuplicateJobError(job.project, job.name) from exc
                raise
            await db.commit()
        return job.job_id

    async def update_job(self, job: Job) -> None:
        cursor = await self._execute_write(
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
        )
        self._raise_if_missing_job(cursor, job.job_id)

    async def update_job_status(self, job: Job) -> None:
        cursor = await self._execute_write(
            """UPDATE jobs SET status=?, updated_at=?,
               current_run_id=?
               WHERE job_id=?""",
            (
                job.status.value,
                fmt_dt(job.updated_at),
                job.current_run_id,
                job.job_id,
            ),
        )
        self._raise_if_missing_job(cursor, job.job_id)

    async def update_job_schedule_state(self, job: Job) -> None:
        cursor = await self._execute_write(
            """UPDATE jobs SET schedule_cron=?, schedule_enabled=?,
               updated_at=?
               WHERE job_id=?""",
            (
                job.schedule_cron,
                int(job.schedule_enabled),
                fmt_dt(job.updated_at),
                job.job_id,
            ),
        )
        self._raise_if_missing_job(cursor, job.job_id)

    async def get_job(self, job_id: str) -> Job:
        async with get_db("jobs.db") as db:
            cursor = await db.execute(
                f"SELECT {_JOB_COLUMNS} FROM jobs WHERE job_id=?",
                (job_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            raise KeyError(f"Job not found: {job_id!r}")
        return _row_to_job(row)

    async def list_jobs(self, project: str | None = None) -> list[Job]:
        async with get_db("jobs.db") as db:
            if project is not None:
                cursor = await db.execute(
                    f"SELECT {_JOB_COLUMNS} FROM jobs WHERE project=?",
                    (project,),
                )
            else:
                cursor = await db.execute(
                    f"SELECT {_JOB_COLUMNS} FROM jobs"
                )
            rows = await cursor.fetchall()
        return [_row_to_job(r) for r in rows]

    async def get_job_runs(self, job_id: str, limit: int = 10) -> list[JobRun]:
        """Return the last N runs for a job, newest first."""
        async with get_db("jobs.db") as db:
            cursor = await db.execute(
                f"SELECT {_JOB_RUN_COLUMNS} FROM job_runs WHERE job_id = ? "
                "ORDER BY started_at DESC LIMIT ?",
                (job_id, limit),
            )
            return [_row_to_job_run(row) for row in await cursor.fetchall()]

    async def get_job_run_stats(
        self, job_id: str,
    ) -> tuple[int, datetime | None]:
        """Return (run_count, last_run_at) derived from job_runs."""
        async with get_db("jobs.db") as db:
            cursor = await db.execute(
                "SELECT COUNT(*), MAX(started_at) "
                "FROM job_runs WHERE job_id = ?",
                (job_id,),
            )
            row = await cursor.fetchone()
            count = row[0] if row else 0
            last = (
                datetime.fromisoformat(row[1]) if row and row[1] else None
            )
            return count, last

    async def list_jobs_with_stats(
        self,
        project: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[tuple[Job, int, datetime | None]]:
        """List jobs with derived run_count and last_run_at, newest activity first."""
        async with get_db("jobs.db") as db:
            job_cols = ", ".join(f"j.{c.strip()}" for c in _JOB_COLUMNS.split(","))
            sql = (
                "WITH job_stats AS ("
                "    SELECT job_id, COUNT(run_id) AS run_count, "
                "           MAX(started_at) AS last_run_at "
                "    FROM job_runs "
                "    GROUP BY job_id"
                ") "
                f"SELECT {job_cols}, "
                "COALESCE(s.run_count, 0) AS run_count, "
                "s.last_run_at "
                "FROM jobs j "
                "LEFT JOIN job_stats s ON j.job_id = s.job_id"
            )
            params: list[object] = []
            if project:
                sql += " WHERE j.project = ?"
                params.append(project)
            sql += (
                " ORDER BY COALESCE(s.last_run_at, j.updated_at, j.created_at) DESC, "
                "j.created_at DESC, j.job_id DESC"
            )
            if limit is not None:
                sql += " LIMIT ? OFFSET ?"
                params.extend([limit, offset])
            elif offset > 0:
                sql += " LIMIT -1 OFFSET ?"
                params.append(offset)
            cursor = await db.execute(sql, params)
            rows = await cursor.fetchall()
            result = []
            for row in rows:
                job = _row_to_job(row[:10])
                run_count = row[10]
                last_run_at = (
                    datetime.fromisoformat(row[11])
                    if row[11]
                    else None
                )
                result.append((job, run_count, last_run_at))
            return result

    async def summary_by_project(self) -> list[tuple[str, str, int]]:
        """Return grouped job counts by project and status."""
        async with get_db("jobs.db") as db:
            cursor = await db.execute(
                "SELECT project, status, COUNT(*) FROM jobs GROUP BY project, status"
            )
            return [
                (row[0], row[1], row[2])
                for row in await cursor.fetchall()
            ]

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
                fmt_dt(datetime.now(timezone.utc)),
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
                fmt_dt(datetime.now(timezone.utc)),
                run_id,
            ),
        )

    async def list_scheduled_jobs(self) -> list[tuple[str, str, bool]]:
        """Return (job_id, schedule_cron, schedule_enabled) for all scheduled jobs."""
        async with get_db("jobs.db") as db:
            cursor = await db.execute(
                "SELECT job_id, schedule_cron, schedule_enabled "
                "FROM jobs WHERE schedule_cron IS NOT NULL"
            )
            return [
                (row[0], row[1], bool(row[2]))
                for row in await cursor.fetchall()
            ]

    async def delete_job(self, job_id: str) -> None:
        async with get_db("jobs.db") as db:
            await db.execute("DELETE FROM job_runs WHERE job_id=?", (job_id,))
            await db.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))
            await db.commit()
