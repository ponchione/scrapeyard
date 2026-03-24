"""SQLite-backed implementation of the JobStore protocol."""

from __future__ import annotations

from datetime import datetime

import aiosqlite

from scrapeyard.common.dt import fmt_dt, parse_dt
from scrapeyard.models.job import Job, JobRun, JobStatus
from scrapeyard.storage.database import get_db


class DuplicateJobError(Exception):
    """Raised when a job name already exists within a project namespace."""

    def __init__(self, project: str, name: str) -> None:
        self.project = project
        self.name = name
        super().__init__(f"Job {name!r} already exists in project {project!r}")


def _row_to_job(row: tuple) -> Job:
    return Job(
        job_id=row[0],
        project=row[1],
        name=row[2],
        status=JobStatus(row[3]),
        config_yaml=row[4],
        created_at=parse_dt(row[5]),  # type: ignore[arg-type]
        updated_at=parse_dt(row[6]),
        schedule_cron=row[7],
        schedule_enabled=bool(row[8]),
        current_run_id=row[9],
    )


def _row_to_job_run(row: aiosqlite.Row) -> JobRun:
    return JobRun(
        run_id=row[0], job_id=row[1], status=row[2], trigger=row[3],
        config_hash=row[4], started_at=parse_dt(row[5]),  # type: ignore[arg-type]
        completed_at=parse_dt(row[6]), record_count=row[7], error_count=row[8],
    )


class SQLiteJobStore:
    """SQLite implementation of :class:`~scrapeyard.storage.protocols.JobStore`."""

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
        async with get_db("jobs.db") as db:
            cursor = await db.execute(
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
            await db.commit()
            if cursor.rowcount == 0:
                raise KeyError(f"Job not found: {job.job_id!r}")

    async def get_job(self, job_id: str) -> Job:
        async with get_db("jobs.db") as db:
            cursor = await db.execute(
                "SELECT job_id, project, name, status, config_yaml, "
                "created_at, updated_at, schedule_cron, schedule_enabled, "
                "current_run_id "
                "FROM jobs WHERE job_id=?",
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
                    "SELECT job_id, project, name, status, config_yaml, "
                    "created_at, updated_at, schedule_cron, schedule_enabled, "
                    "current_run_id "
                    "FROM jobs WHERE project=?",
                    (project,),
                )
            else:
                cursor = await db.execute(
                    "SELECT job_id, project, name, status, config_yaml, "
                    "created_at, updated_at, schedule_cron, schedule_enabled, "
                    "current_run_id "
                    "FROM jobs"
                )
            rows = await cursor.fetchall()
        return [_row_to_job(r) for r in rows]

    async def get_job_runs(self, job_id: str, limit: int = 10) -> list[JobRun]:
        """Return the last N runs for a job, newest first."""
        async with get_db("jobs.db") as db:
            cursor = await db.execute(
                "SELECT run_id, job_id, status, trigger, config_hash, "
                "started_at, completed_at, record_count, error_count "
                "FROM job_runs WHERE job_id = ? "
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
        self, project: str | None = None,
    ) -> list[tuple[Job, int, datetime | None]]:
        """List jobs with derived run_count and last_run_at via LEFT JOIN."""
        async with get_db("jobs.db") as db:
            # Build the column list matching _row_to_job field order
            job_cols = (
                "j.job_id, j.project, j.name, j.status, j.config_yaml, "
                "j.created_at, j.updated_at, j.schedule_cron, "
                "j.schedule_enabled, j.current_run_id"
            )
            sql = (
                f"SELECT {job_cols}, COUNT(r.run_id) AS run_count, "
                "MAX(r.started_at) AS last_run_at "
                "FROM jobs j LEFT JOIN job_runs r ON j.job_id = r.job_id"
            )
            params: list[str] = []
            if project:
                sql += " WHERE j.project = ?"
                params.append(project)
            sql += " GROUP BY j.job_id"
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

    async def delete_job(self, job_id: str) -> None:
        async with get_db("jobs.db") as db:
            await db.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))
            await db.commit()
