"""SQLite-backed implementation of the JobStore protocol."""

from __future__ import annotations

from scrapeyard.common.dt import fmt_dt, parse_dt
from scrapeyard.models.job import Job, JobStatus
from scrapeyard.storage.database import get_db


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
        last_run_at=parse_dt(row[8]),
        run_count=row[9],
        current_run_id=row[10],
    )


class SQLiteJobStore:
    """SQLite implementation of :class:`~scrapeyard.storage.protocols.JobStore`."""

    async def save_job(self, job: Job) -> str:
        async with get_db("jobs.db") as db:
            await db.execute(
                """INSERT INTO jobs (job_id, project, name, status, config_yaml,
                   created_at, updated_at, schedule_cron, last_run_at, run_count, current_run_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job.job_id,
                    job.project,
                    job.name,
                    job.status.value,
                    job.config_yaml,
                    fmt_dt(job.created_at),
                    fmt_dt(job.updated_at),
                    job.schedule_cron,
                    fmt_dt(job.last_run_at),
                    job.run_count,
                    job.current_run_id,
                ),
            )
            await db.commit()
        return job.job_id

    async def update_job(self, job: Job) -> None:
        async with get_db("jobs.db") as db:
            cursor = await db.execute(
                """UPDATE jobs SET project=?, name=?, status=?, config_yaml=?,
                   created_at=?, updated_at=?, schedule_cron=?, last_run_at=?, run_count=?, current_run_id=?
                   WHERE job_id=?""",
                (
                    job.project,
                    job.name,
                    job.status.value,
                    job.config_yaml,
                    fmt_dt(job.created_at),
                    fmt_dt(job.updated_at),
                    job.schedule_cron,
                    fmt_dt(job.last_run_at),
                    job.run_count,
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
                "created_at, updated_at, schedule_cron, last_run_at, run_count, current_run_id "
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
                    "created_at, updated_at, schedule_cron, last_run_at, run_count, current_run_id "
                    "FROM jobs WHERE project=?",
                    (project,),
                )
            else:
                cursor = await db.execute(
                    "SELECT job_id, project, name, status, config_yaml, "
                    "created_at, updated_at, schedule_cron, last_run_at, run_count, current_run_id "
                    "FROM jobs"
                )
            rows = await cursor.fetchall()
        return [_row_to_job(r) for r in rows]

    async def delete_job(self, job_id: str) -> None:
        async with get_db("jobs.db") as db:
            await db.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))
            await db.commit()
