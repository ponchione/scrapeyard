"""APScheduler integration for cron-based scheduled scrape jobs."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from contextlib import suppress
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.schedulers.base import SchedulerNotRunningError
from apscheduler.triggers.cron import CronTrigger

from scrapeyard.common.ids import generate_run_id
from scrapeyard.common.time import utc_now
from scrapeyard.config.loader import load_config
from scrapeyard.models.job import Job, JobStatus
from scrapeyard.runtime.metrics import mark_last_success

from scrapeyard.queue.delivery import queue_delivery_metadata
from scrapeyard.queue.job_state import run_lease_is_active
from scrapeyard.queue.pool import WorkerPool
from scrapeyard.queue.terminal_reconciliation import reconcile_terminal_webhook_intents
from scrapeyard.storage.protocols import JobStore, ResultStore

logger = logging.getLogger(__name__)


class ManualTriggerConflictError(RuntimeError):
    """Raised when an accepted/current run prevents a manual trigger."""


class ScheduledJobLifecycleError(RuntimeError):
    """Raised when a job cannot participate in scheduled execution."""


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
        job_store: JobStore,
        jitter_max_seconds: int = 120,
        queued_claim_timeout_seconds: int = 300,
        running_heartbeat_timeout_seconds: int = 600,
        result_store: ResultStore | None = None,
        misfire_grace_seconds: int = 60,
    ) -> None:
        self._pool = worker_pool
        self._job_store = job_store
        self._jitter_max = jitter_max_seconds
        self._queued_claim_timeout_seconds = queued_claim_timeout_seconds
        self._running_heartbeat_timeout_seconds = running_heartbeat_timeout_seconds
        self._result_store = result_store
        self._misfire_grace_seconds = misfire_grace_seconds
        self._scheduler = AsyncIOScheduler()

    def register_job(
        self,
        job_id: str,
        cron_expr: str,
        *,
        timezone_name: str = "UTC",
        enabled: bool = True,
    ) -> None:
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
        trigger = CronTrigger.from_crontab(
            cron_expr,
            timezone=ZoneInfo(timezone_name),
        )
        trigger.jitter = self._jitter_max

        # Remove existing job with same id if present.
        with suppress(JobLookupError):
            self._scheduler.remove_job(job_id)

        self._scheduler.add_job(
            self._trigger_job,
            trigger=trigger,
            id=job_id,
            args=[job_id],
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=self._misfire_grace_seconds,
        )

        if not enabled:
            self._scheduler.pause_job(job_id)

    def remove_job(self, job_id: str) -> None:
        """Remove a scheduled job. Silent if the job doesn't exist."""
        with suppress(JobLookupError):
            self._scheduler.remove_job(job_id)

    async def start(self) -> None:
        """Start the scheduler and re-register all enabled scheduled jobs from the store."""
        rows = await self._job_store.list_scheduled_jobs()

        for job_id, cron_expr, timezone_name, schedule_enabled in rows:
            self.register_job(
                job_id,
                cron_expr,
                timezone_name=timezone_name,
                enabled=schedule_enabled,
            )

        self._scheduler.start()
        mark_last_success("scheduler")

    def shutdown(self) -> None:
        """Gracefully stop the scheduler."""
        with suppress(SchedulerNotRunningError):
            self._scheduler.shutdown(wait=False)

    @property
    def background_ok(self) -> bool:
        """Whether APScheduler's process-local loop is running."""

        return bool(self._scheduler.running)

    @property
    def background_detail(self) -> str | None:
        return None if self.background_ok else "scheduler is stopped"

    async def trigger_job_now(self, job_id: str) -> tuple[str, str]:
        """Queue one manual run and return its run/config version identity."""

        try:
            job = await self._job_store.get_job(job_id)
        except KeyError:
            raise
        if job.schedule_cron is None:
            raise ScheduledJobLifecycleError("Job is not scheduled")
        if job.status in {JobStatus.cancelled, JobStatus.deleting}:
            raise ScheduledJobLifecycleError(
                f"Job lifecycle state {job.status.value!r} cannot be triggered"
            )
        if await self._job_has_active_run(job, now=utc_now()):
            raise ManualTriggerConflictError("Job already has a queued or active run")
        result = await self._trigger_job(
            job_id,
            trigger="manual",
            raise_enqueue_errors=True,
        )
        if result is None:
            raise ManualTriggerConflictError(
                "Job state changed before the manual run could be queued"
            )
        return result

    async def _trigger_job(
        self,
        job_id: str,
        *,
        trigger: str = "scheduled",
        raise_enqueue_errors: bool = False,
    ) -> tuple[str, str] | None:
        """Called by APScheduler when a cron trigger fires.

        Jitter is already applied by the CronTrigger — no additional delay needed.
        """
        try:
            job = await self._job_store.get_job(job_id)
        except KeyError:
            # Job was deleted — remove from scheduler.
            self.remove_job(job_id)
            return None

        if job.status in {JobStatus.cancelled, JobStatus.deleting}:
            logger.info(
                "Scheduled trigger blocked by lifecycle state job_id=%s run_id=%s "
                "status=%s recovery_action=remove_local_schedule",
                job_id,
                job.current_run_id,
                job.status.value,
            )
            self.remove_job(job_id)
            return None

        if trigger == "scheduled" and not job.schedule_enabled:
            logger.info(
                "Ignoring disabled scheduled trigger job_id=%s recovery_action=no_op",
                job_id,
            )
            return None

        now = utc_now()
        if await self._job_has_active_run(job, now=now):
            return None

        if job.status == JobStatus.running:
            run_id = job.current_run_id
            if run_id is None:
                logger.warning(
                    "Recovering running job without run ownership job_id=%s run_id=None",
                    job_id,
                )
                recovered = await self._job_store.recover_stale_run(
                    job_id,
                    None,
                    now - timedelta(seconds=self._running_heartbeat_timeout_seconds),
                    now,
                )
                if not recovered:
                    return None
            else:
                recovered = await self._job_store.recover_stale_run(
                    job_id,
                    run_id,
                    now - timedelta(seconds=self._running_heartbeat_timeout_seconds),
                    now,
                )
                if not recovered:
                    logger.info(
                        "Scheduled stale recovery became a no-op job_id=%s run_id=%s "
                        "timeout_seconds=%s recovery_action=no_op",
                        job_id,
                        run_id,
                        self._running_heartbeat_timeout_seconds,
                    )
                    return None
                logger.warning(
                    "Recovered stale scheduled run job_id=%s run_id=%s "
                    "timeout_seconds=%s recovery_action=failed_stale_run",
                    job_id,
                    run_id,
                    self._running_heartbeat_timeout_seconds,
                )
            if self._result_store is not None:
                await reconcile_terminal_webhook_intents(
                    job_store=self._job_store,
                    result_store=self._result_store,
                )
            job = await self._job_store.get_job(job_id)

        config = await asyncio.to_thread(load_config, job.config_yaml)
        delivery = queue_delivery_metadata(config)
        run_id = generate_run_id()
        stale_before = None
        if job.status == JobStatus.queued and job.current_run_id is not None:
            stale_before = now - timedelta(seconds=self._queued_claim_timeout_seconds)
        queued = await self._job_store.queue_run(
            job_id,
            expected_status=job.status.value,
            expected_run_id=job.current_run_id,
            new_run_id=run_id,
            new_trigger=trigger,
            queued_at=now,
            stale_before=stale_before,
            expected_config_yaml=job.config_yaml,
        )
        if not queued:
            logger.info(
                "Scheduled queue replacement became a no-op job_id=%s "
                "expected_run_id=%s new_run_id=%s ownership_outcome=lost",
                job_id,
                job.current_run_id,
                run_id,
            )
            return None
        try:
            await self._pool.enqueue(
                job.job_id,
                job.config_yaml,
                delivery.priority,
                delivery.needs_browser,
                run_id=run_id,
                trigger=trigger,
            )
        except Exception:
            logger.exception("Failed to enqueue scheduled job %s", job.job_id)
            failed = await self._job_store.fail_queued_run(job_id, run_id, utc_now())
            if not failed:
                logger.info(
                    "Skipping failed enqueue mutation after ownership loss "
                    "job_id=%s run_id=%s",
                    job_id,
                    run_id,
                )
            if raise_enqueue_errors:
                raise
            return None
        mark_last_success("scheduler")
        return run_id, hashlib.sha256(job.config_yaml.encode("utf-8")).hexdigest()

    def get_next_run_time(self, job_id: str) -> datetime | None:
        """Return the next scheduled fire time, or None if not scheduled."""
        aps_job = self._scheduler.get_job(job_id)
        return aps_job.next_run_time if aps_job else None

    async def _job_has_active_run(self, job: Job, *, now: datetime) -> bool:
        status = job.status
        if status not in {JobStatus.queued, JobStatus.running}:
            return False
        if status == JobStatus.queued and job.current_run_id is None:
            return False
        if status == JobStatus.queued:
            active = run_lease_is_active(
                job.updated_at,
                lease_seconds=self._queued_claim_timeout_seconds,
                now=now,
            )
            if active:
                logger.info(
                    "Skipping scheduled trigger for queued delivery job_id=%s run_id=%s "
                    "queued_timeout_seconds=%s",
                    job.job_id,
                    job.current_run_id,
                    self._queued_claim_timeout_seconds,
                )
            return active

        run_id = job.current_run_id
        if run_id is None:
            return False
        run = await self._job_store.get_job_run(job.job_id, run_id)
        if run is None or run.status != JobStatus.running:
            return False
        active = run_lease_is_active(
            run.heartbeat_at,
            lease_seconds=self._running_heartbeat_timeout_seconds,
            now=now,
        )
        if active:
            logger.info(
                "Skipping scheduled trigger for healthy run job_id=%s run_id=%s "
                "last_heartbeat=%s timeout_seconds=%s recovery_action=skip",
                job.job_id,
                run_id,
                run.heartbeat_at.isoformat(),
                self._running_heartbeat_timeout_seconds,
            )
        return active
