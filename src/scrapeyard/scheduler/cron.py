"""APScheduler integration for cron-based scheduled scrape jobs."""

from __future__ import annotations

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from scrapeyard.config.loader import load_config
from scrapeyard.config.schema import FetcherType

from scrapeyard.queue.pool import WorkerPool
from scrapeyard.storage.job_store import SQLiteJobStore


class SchedulerService:
    """Wraps APScheduler to manage cron-triggered scrape jobs.

    Parameters
    ----------
    worker_pool:
        The worker pool to enqueue jobs into.
    job_store:
        The job store for loading scheduled jobs on startup.
    jitter_max_seconds:
        Maximum random jitter added to each trigger (in seconds).
    """

    def __init__(
        self,
        worker_pool: WorkerPool,
        job_store: SQLiteJobStore,
        jitter_max_seconds: int = 120,
    ) -> None:
        self._pool = worker_pool
        self._job_store = job_store
        self._jitter_max = jitter_max_seconds
        self._scheduler = AsyncIOScheduler()

    def register_job(self, job_id: str, cron_expr: str, enabled: bool = True) -> None:
        """Add or replace a cron-triggered job in the scheduler.

        Parameters
        ----------
        job_id:
            The job ID used as the APScheduler job identifier.
        cron_expr:
            A cron expression (5-field) for the trigger.
        enabled:
            If False, the job is added in a paused state.
        """
        trigger = CronTrigger.from_crontab(cron_expr)
        trigger.jitter = self._jitter_max

        # Remove existing job with same id if present.
        try:
            self._scheduler.remove_job(job_id)
        except JobLookupError:
            pass

        self._scheduler.add_job(
            self._trigger_job,
            trigger=trigger,
            id=job_id,
            args=[job_id],
            replace_existing=True,
        )

        if not enabled:
            self._scheduler.pause_job(job_id)

    def remove_job(self, job_id: str) -> None:
        """Remove a scheduled job. Silent if the job doesn't exist."""
        try:
            self._scheduler.remove_job(job_id)
        except JobLookupError:
            pass

    async def start(self) -> None:
        """Start the scheduler and re-register all enabled scheduled jobs from the store."""
        # Re-register persisted scheduled jobs.
        # We need to scan all projects; use a direct DB query.
        from scrapeyard.storage.database import get_db

        async with get_db("jobs.db") as db:
            cursor = await db.execute(
                "SELECT job_id, schedule_cron FROM jobs WHERE schedule_cron IS NOT NULL"
            )
            rows = await cursor.fetchall()

        for job_id, cron_expr in rows:
            self.register_job(job_id, cron_expr, enabled=True)

        self._scheduler.start()

    def shutdown(self) -> None:
        """Gracefully stop the scheduler."""
        self._scheduler.shutdown(wait=False)

    async def _trigger_job(self, job_id: str) -> None:
        """Called by APScheduler when a cron trigger fires.

        Jitter is already applied by the CronTrigger — no additional delay needed.
        """
        try:
            job = await self._job_store.get_job(job_id)
        except KeyError:
            # Job was deleted — remove from scheduler.
            self.remove_job(job_id)
            return

        config = load_config(job.config_yaml)
        priority = config.execution.priority.value
        needs_browser = any(t.fetcher != FetcherType.basic for t in config.resolved_targets())
        await self._pool.enqueue(job.job_id, job.config_yaml, priority, needs_browser)
