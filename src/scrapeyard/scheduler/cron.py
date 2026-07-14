"""APScheduler integration for cron-based scheduled scrape jobs."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from contextlib import suppress
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import yaml
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
from scrapeyard.queue.reconciliation import (
    QueuedReconciliationError,
    reconcile_stale_queued_job,
)
from scrapeyard.queue.terminal_reconciliation import reconcile_terminal_webhook_intents
from scrapeyard.storage.protocols import JobStore, ResultStore
from scrapeyard.storage.secret_envelope import (
    SecretDecryptionError,
    SecretKeyConfigurationError,
)
from scrapeyard.storage.types import StaleQueuedJob

logger = logging.getLogger(__name__)


class ManualTriggerConflictError(RuntimeError):
    """Raised when an accepted/current run prevents a manual trigger."""


class SchedulerUnavailableError(RuntimeError):
    """Raised when authoritative queue state cannot be inspected safely."""


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
        self._background_error: str | None = None
        self._schedule_failures: dict[str, str] = {}

    def register_job(
        self,
        job_id: str,
        cron_expr: str,
        *,
        timezone_name: str = "UTC",
        enabled: bool = True,
        failure_code: str | None = None,
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
            self._run_scheduled_callback,
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
            self._schedule_failures.pop(job_id, None)
        elif failure_code is None:
            self._schedule_failures.pop(job_id, None)
        else:
            self._schedule_failures[job_id] = failure_code

    def remove_job(self, job_id: str) -> None:
        """Remove a scheduled job. Silent if the job doesn't exist."""
        with suppress(JobLookupError):
            self._scheduler.remove_job(job_id)
        self._schedule_failures.pop(job_id, None)

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

        self._schedule_failures = dict(
            await self._job_store.list_scheduled_trigger_failures()
        )

        self._scheduler.start()
        self._background_error = None
        mark_last_success("scheduler")

    def shutdown(self) -> None:
        """Gracefully stop the scheduler."""
        with suppress(SchedulerNotRunningError):
            self._scheduler.shutdown(wait=False)

    @property
    def background_ok(self) -> bool:
        """Whether APScheduler's process-local loop is running."""

        return (
            bool(self._scheduler.running)
            and self._background_error is None
            and not self._schedule_failures
        )

    @property
    def background_detail(self) -> str | None:
        if self._background_error is not None:
            return self._background_error
        if self._schedule_failures:
            job_id = sorted(self._schedule_failures)[0]
            return (
                "scheduled callback failures: "
                f"count={len(self._schedule_failures)} "
                f"job_id={job_id} code={self._schedule_failures[job_id]}"
            )
        return None if self.background_ok else "scheduler is stopped"

    @staticmethod
    def _failure_code(exc: Exception) -> str:
        if isinstance(exc, SchedulerUnavailableError):
            return "queue_state_unavailable"
        if isinstance(exc, MemoryError):
            return "enqueue_resource_exhausted"
        if isinstance(exc, (SecretDecryptionError, SecretKeyConfigurationError)):
            return "persisted_config_unavailable"
        if isinstance(exc, (ValueError, yaml.YAMLError)):
            return "persisted_config_invalid"
        return "scheduled_callback_failed"

    async def _run_scheduled_callback(self, job_id: str) -> None:
        """Contain, sanitize, and durably record one APScheduler callback."""

        run_id = generate_run_id()
        try:
            result = await self._trigger_job(
                job_id,
                trigger="scheduled",
                new_run_id=run_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure_code = self._failure_code(exc)
            try:
                persisted = await self._job_store.record_scheduled_trigger_failure(
                    job_id,
                    run_id,
                    failed_at=utc_now(),
                    failure_code=failure_code,
                )
            except Exception as persistence_exc:
                self._schedule_failures[job_id] = failure_code
                logger.error(
                    "Scheduled callback failure state could not be persisted "
                    "job_id=%s run_id=%s failure_code=%s callback_error_type=%s "
                    "persistence_error_type=%s recovery_action=retry_next_fire",
                    job_id,
                    run_id,
                    failure_code,
                    type(exc).__name__,
                    type(persistence_exc).__name__,
                )
            else:
                if persisted:
                    self._schedule_failures[job_id] = failure_code
                else:
                    self._schedule_failures.pop(job_id, None)
                logger.error(
                    "Scheduled callback failed job_id=%s run_id=%s failure_code=%s "
                    "error_type=%s persisted=%s recovery_action=retry_next_fire",
                    job_id,
                    run_id,
                    failure_code,
                    type(exc).__name__,
                    persisted,
                )
            return

        if result is None:
            return
        try:
            await self._job_store.clear_scheduled_trigger_failure(job_id)
        except Exception as exc:
            self._schedule_failures[job_id] = "schedule_health_persistence_failed"
            logger.error(
                "Accepted scheduled run but failed to clear degraded health "
                "job_id=%s run_id=%s error_type=%s recovery_action=retry_next_fire",
                job_id,
                result[0],
                type(exc).__name__,
            )
            return
        self._schedule_failures.pop(job_id, None)
        self._background_error = None
        mark_last_success("scheduler")

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
        new_run_id: str | None = None,
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
        try:
            if await self._job_has_active_run(job, now=now):
                return None
        except SchedulerUnavailableError:
            raise

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
        run_id = new_run_id or generate_run_id()
        queued = await self._job_store.queue_run(
            job_id,
            expected_status=job.status.value,
            expected_run_id=job.current_run_id,
            new_run_id=run_id,
            new_trigger=trigger,
            queued_at=now,
            stale_before=None,
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
        except Exception as exc:
            logger.error(
                "Failed to enqueue scheduled job job_id=%s run_id=%s error_type=%s",
                job.job_id,
                run_id,
                type(exc).__name__,
            )
            failed = await self._job_store.fail_queued_run(job_id, run_id, utc_now())
            if not failed:
                logger.info(
                    "Skipping failed enqueue mutation after ownership loss "
                    "job_id=%s run_id=%s",
                    job_id,
                    run_id,
                )
            if trigger == "scheduled" or raise_enqueue_errors:
                raise
            return None
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
            if job.updated_at is None:
                logger.error(
                    "Queued delivery lacks a persisted timestamp; failing closed "
                    "job_id=%s run_id=%s recovery_action=skip_trigger",
                    job.job_id,
                    job.current_run_id,
                )
                return True
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
                return True
            await self._reconcile_stale_queued_run(job, now=now)
            return True

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

    async def _reconcile_stale_queued_run(self, job: Job, *, now: datetime) -> None:
        """Recover one stale queued owner without changing its accepted run ID."""

        run_id = job.current_run_id
        queued_at = job.updated_at
        if run_id is None or queued_at is None:
            return
        trigger = job.current_trigger
        if trigger is None:
            trigger = "scheduled" if job.schedule_cron is not None else "adhoc"
        stale_before = now - timedelta(seconds=self._queued_claim_timeout_seconds)
        queued_job = StaleQueuedJob(
            job_id=job.job_id,
            run_id=run_id,
            config_yaml=job.config_yaml,
            queued_at=queued_at,
            trigger=trigger,
            schedule_cron=job.schedule_cron,
            schedule_enabled=job.schedule_enabled,
        )
        try:
            await reconcile_stale_queued_job(
                queued_job,
                job_store=self._job_store,
                worker_pool=self._pool,
                queued_claim_timeout_seconds=self._queued_claim_timeout_seconds,
                stale_before=stale_before,
                inspected_at=now,
            )
        except QueuedReconciliationError as exc:
            logger.error(
                "Scheduled trigger could not inspect authoritative Redis state "
                "job_id=%s run_id=%s recovery_action=fail_closed",
                job.job_id,
                run_id,
            )
            raise SchedulerUnavailableError(
                "Authoritative queue state is temporarily unavailable"
            ) from exc
