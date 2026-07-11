"""Startup reconciliation for SQLite queued state and arq delivery state."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from scrapeyard.common.time import utc_now
from scrapeyard.config.loader import load_config
from scrapeyard.queue.delivery import queue_delivery_metadata
from scrapeyard.queue.pool import QueueDeliveryState, WorkerPool
from scrapeyard.storage.protocols import JobStore
from scrapeyard.storage.types import StaleQueuedJob

logger = logging.getLogger(__name__)

_ACTIVE_DELIVERY_STATES = {
    QueueDeliveryState.queued,
    QueueDeliveryState.deferred,
    QueueDeliveryState.in_progress,
}


class QueuedReconciliationError(RuntimeError):
    """Raised when startup cannot safely inspect authoritative Redis state."""


@dataclass(frozen=True, slots=True)
class QueuedReconciliationSummary:
    """Operational counts from one complete startup reconciliation pass."""

    inspected: int = 0
    queue_present: int = 0
    recovered: int = 0
    failed: int = 0
    race_noop: int = 0


def _queued_age_seconds(job: StaleQueuedJob, now: datetime) -> float:
    queued_at = job.queued_at
    comparable_now = now.replace(tzinfo=None) if queued_at.tzinfo is None else now
    return max(0.0, (comparable_now - queued_at).total_seconds())


def _policy(job: StaleQueuedJob) -> str:
    if job.trigger == "scheduled":
        return "scheduled_reenqueue_accepted_delivery_original_run"
    return "adhoc_reenqueue_original_run"


def _log_race_noop(
    job: StaleQueuedJob,
    *,
    now: datetime,
    timeout_seconds: int,
    action: str,
) -> None:
    logger.info(
        "Queued reconciliation CAS no-op job_id=%s run_id=%s "
        "queued_age_seconds=%.3f timeout_seconds=%s trigger=%s "
        "recovery_policy=%s recovery_action=%s ownership_outcome=lost",
        job.job_id,
        job.run_id,
        _queued_age_seconds(job, now),
        timeout_seconds,
        job.trigger,
        _policy(job),
        action,
    )


async def _conditionally_fail_delivery(
    job: StaleQueuedJob,
    *,
    job_store: JobStore,
    failed_at: datetime,
    timeout_seconds: int,
    action: str,
    expected_queued_at: datetime | None = None,
) -> bool:
    failed = await job_store.fail_queued_run_recovery(
        job.job_id,
        job.run_id,
        expected_queued_at=expected_queued_at or job.queued_at,
        failed_at=failed_at,
    )
    if not failed:
        _log_race_noop(
            job,
            now=failed_at,
            timeout_seconds=timeout_seconds,
            action=f"{action}_race_noop",
        )
        return False
    logger.error(
        "Queued reconciliation conditionally failed delivery job_id=%s run_id=%s "
        "queued_age_seconds=%.3f timeout_seconds=%s trigger=%s "
        "recovery_policy=%s recovery_action=%s",
        job.job_id,
        job.run_id,
        _queued_age_seconds(job, failed_at),
        timeout_seconds,
        job.trigger,
        _policy(job),
        action,
    )
    return True


async def reconcile_stale_queued_jobs(
    *,
    job_store: JobStore,
    worker_pool: WorkerPool,
    queued_claim_timeout_seconds: int,
    now: datetime | None = None,
) -> QueuedReconciliationSummary:
    """Reconcile stale SQLite queue ownership against direct arq state.

    Both ad-hoc and scheduled deliveries retain the accepted ``run_id``. A
    scheduled job that has since been disabled still recovers this already
    accepted occurrence; ``schedule_enabled`` controls only future cron fires.
    """
    inspected_at = now or utc_now()
    stale_before = inspected_at - timedelta(seconds=queued_claim_timeout_seconds)
    stale_jobs = await job_store.list_stale_queued_jobs(stale_before)
    logger.info(
        "Queued reconciliation inspected stale_count=%s timeout_seconds=%s "
        "recovery_action=inspection_complete",
        len(stale_jobs),
        queued_claim_timeout_seconds,
    )

    queue_present = 0
    recovered = 0
    failed = 0
    race_noop = 0

    for job in stale_jobs:
        age = _queued_age_seconds(job, inspected_at)
        try:
            delivery_state = await worker_pool.inspect_delivery(job.run_id)
        except Exception as exc:
            logger.error(
                "Redis queued-delivery inspection failed job_id=%s run_id=%s "
                "queued_age_seconds=%.3f timeout_seconds=%s trigger=%s "
                "recovery_policy=%s recovery_action=abort_startup error_type=%s",
                job.job_id,
                job.run_id,
                age,
                queued_claim_timeout_seconds,
                job.trigger,
                _policy(job),
                type(exc).__name__,
            )
            raise QueuedReconciliationError(
                "Redis delivery inspection failed; scheduler startup aborted "
                f"for job_id={job.job_id!r} run_id={job.run_id!r}"
            ) from exc

        if delivery_state in _ACTIVE_DELIVERY_STATES:
            queue_present += 1
            logger.info(
                "Queued reconciliation found active delivery job_id=%s run_id=%s "
                "queued_age_seconds=%.3f timeout_seconds=%s trigger=%s "
                "recovery_policy=%s redis_state=%s recovery_action=queue_present_noop",
                job.job_id,
                job.run_id,
                age,
                queued_claim_timeout_seconds,
                job.trigger,
                _policy(job),
                delivery_state.value,
            )
            continue

        if delivery_state is QueueDeliveryState.complete:
            did_fail = await _conditionally_fail_delivery(
                job,
                job_store=job_store,
                failed_at=inspected_at,
                timeout_seconds=queued_claim_timeout_seconds,
                action="failed_completed_arq_record_without_sqlite_claim",
            )
            failed += int(did_fail)
            race_noop += int(not did_fail)
            continue

        logger.warning(
            "Queued reconciliation found missing delivery job_id=%s run_id=%s "
            "queued_age_seconds=%.3f timeout_seconds=%s trigger=%s "
            "recovery_policy=%s redis_state=%s recovery_action=prepare_reenqueue",
            job.job_id,
            job.run_id,
            age,
            queued_claim_timeout_seconds,
            job.trigger,
            _policy(job),
            delivery_state.value,
        )
        try:
            config = await asyncio.to_thread(load_config, job.config_yaml)
            metadata = queue_delivery_metadata(config)
        except Exception as exc:
            logger.error(
                "Queued recovery config reconstruction failed job_id=%s run_id=%s "
                "queued_age_seconds=%.3f timeout_seconds=%s trigger=%s "
                "recovery_policy=%s recovery_action=fail_invalid_stored_config "
                "error_type=%s",
                job.job_id,
                job.run_id,
                age,
                queued_claim_timeout_seconds,
                job.trigger,
                _policy(job),
                type(exc).__name__,
            )
            did_fail = await _conditionally_fail_delivery(
                job,
                job_store=job_store,
                failed_at=inspected_at,
                timeout_seconds=queued_claim_timeout_seconds,
                action="failed_unreconstructable_stored_config",
            )
            failed += int(did_fail)
            race_noop += int(not did_fail)
            continue

        reserved = await job_store.reserve_queued_run_recovery(
            job.job_id,
            job.run_id,
            expected_queued_at=job.queued_at,
            stale_before=stale_before,
            reserved_at=inspected_at,
        )
        if not reserved:
            race_noop += 1
            _log_race_noop(
                job,
                now=inspected_at,
                timeout_seconds=queued_claim_timeout_seconds,
                action="reenqueue_original_run",
            )
            continue

        try:
            await worker_pool.enqueue(
                job.job_id,
                job.config_yaml,
                metadata.priority,
                metadata.needs_browser,
                run_id=job.run_id,
                trigger=job.trigger,
            )
        except Exception as exc:
            logger.error(
                "Queued recovery enqueue failed job_id=%s run_id=%s "
                "queued_age_seconds=%.3f timeout_seconds=%s trigger=%s "
                "recovery_policy=%s recovery_action=conditionally_fail_delivery "
                "error_type=%s",
                job.job_id,
                job.run_id,
                age,
                queued_claim_timeout_seconds,
                job.trigger,
                _policy(job),
                type(exc).__name__,
            )
            did_fail = await _conditionally_fail_delivery(
                job,
                job_store=job_store,
                failed_at=inspected_at,
                timeout_seconds=queued_claim_timeout_seconds,
                action="failed_recovery_enqueue",
                expected_queued_at=inspected_at,
            )
            failed += int(did_fail)
            race_noop += int(not did_fail)
            continue

        recovered += 1
        logger.warning(
            "Queued reconciliation restored missing delivery job_id=%s run_id=%s "
            "queued_age_seconds=%.3f timeout_seconds=%s trigger=%s "
            "recovery_policy=%s recovery_action=reenqueued_original_run",
            job.job_id,
            job.run_id,
            age,
            queued_claim_timeout_seconds,
            job.trigger,
            _policy(job),
        )

    summary = QueuedReconciliationSummary(
        inspected=len(stale_jobs),
        queue_present=queue_present,
        recovered=recovered,
        failed=failed,
        race_noop=race_noop,
    )
    logger.info(
        "Queued reconciliation finished inspected_count=%s queue_present_count=%s "
        "recovered_count=%s failed_count=%s race_noop_count=%s "
        "timeout_seconds=%s recovery_action=reconciliation_complete",
        summary.inspected,
        summary.queue_present,
        summary.recovered,
        summary.failed,
        summary.race_noop,
        queued_claim_timeout_seconds,
    )
    return summary
