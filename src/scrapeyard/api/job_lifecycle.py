"""Application service for race-safe cancellation and resumable deletion."""

from __future__ import annotations

import logging
from collections.abc import Awaitable

from scrapeyard.common.time import utc_now
from scrapeyard.queue.cancellation import (
    QueueCancellationOutcome,
    QueueDeliveryState,
)
from scrapeyard.queue.pool import WorkerPool
from scrapeyard.scheduler.cron import SchedulerService
from scrapeyard.storage.protocols import ErrorStore, JobStore, ResultStore
from scrapeyard.storage.types import (
    CancellationAction,
    DeletionFinalizationAction,
    DeletionReservationAction,
)

logger = logging.getLogger(__name__)

_ACTIVE_QUEUE_STATES = {
    QueueDeliveryState.queued,
    QueueDeliveryState.deferred,
    QueueDeliveryState.in_progress,
}


class JobLifecycleRequestError(RuntimeError):
    """Safe public failure returned by cancellation/deletion orchestration."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(message)


async def cancel_current_job(
    job_id: str,
    *,
    job_store: JobStore,
    worker_pool: WorkerPool,
    scheduler: SchedulerService,
) -> None:
    """Durably cancel, disable scheduling, and prove queue quiescence."""

    requested_at = utc_now()
    logger.info(
        "Job cancellation requested job_id=%s cancellation_phase=durable_cas",
        job_id,
    )
    try:
        outcome = await job_store.cancel_job(job_id, requested_at)
    except Exception as exc:
        logger.error(
            "Job cancellation CAS failed job_id=%s cancellation_phase=durable_cas "
            "error_type=%s",
            job_id,
            type(exc).__name__,
        )
        raise JobLifecycleRequestError(
            500,
            "Cancellation could not be persisted; retry the request.",
        ) from exc

    logger.info(
        "Job cancellation CAS outcome job_id=%s run_id=%s prior_status=%s "
        "resulting_status=%s cancellation_action=%s",
        job_id,
        outcome.run_id,
        None if outcome.prior_status is None else outcome.prior_status.value,
        None if outcome.resulting_status is None else outcome.resulting_status.value,
        outcome.action.value,
    )
    if outcome.action is CancellationAction.missing:
        raise JobLifecycleRequestError(404, f"Job {job_id!r} not found")
    if outcome.action is CancellationAction.conflict:
        status = outcome.prior_status.value if outcome.prior_status is not None else "unknown"
        if status == "deleting":
            message = "Job deletion is already in progress and cannot be cancelled."
        else:
            message = f"Job in terminal status {status!r} cannot be cancelled."
        raise JobLifecycleRequestError(409, message)

    scheduler.remove_job(job_id)
    if not outcome.queue_quiescence_required:
        return
    assert outcome.run_id is not None
    queue_result = await worker_pool.cancel_run(outcome.run_id)
    logger.info(
        "Job cancellation queue outcome job_id=%s run_id=%s prior_status=%s "
        "resulting_status=cancelled queue_outcome=%s cancellation_phase=quiescence",
        job_id,
        outcome.run_id,
        None if outcome.prior_status is None else outcome.prior_status.value,
        queue_result.outcome.value,
    )
    if queue_result.outcome is QueueCancellationOutcome.unavailable:
        raise JobLifecycleRequestError(
            503,
            "Job is durably cancelled, but Redis cancellation could not be verified; "
            "retry this cancellation request when Redis is available.",
        )
    if queue_result.outcome is QueueCancellationOutcome.timeout:
        raise JobLifecycleRequestError(
            504,
            "Job is durably cancelled, but worker quiescence timed out; "
            "retry this cancellation request.",
        )


async def delete_reserved_job(
    job_id: str,
    *,
    delete_results: bool,
    job_store: JobStore,
    result_store: ResultStore,
    error_store: ErrorStore,
    worker_pool: WorkerPool,
    scheduler: SchedulerService,
) -> None:
    """Reserve and resume ordered cross-database job deletion."""

    try:
        reservation = await job_store.reserve_job_deletion(
            job_id,
            delete_results=delete_results,
            requested_at=utc_now(),
        )
    except Exception as exc:
        logger.error(
            "Deletion reservation failed job_id=%s deletion_phase=reservation "
            "error_type=%s",
            job_id,
            type(exc).__name__,
        )
        raise JobLifecycleRequestError(
            500,
            "Job deletion could not be reserved; retry the request.",
        ) from exc

    logger.info(
        "Deletion reservation outcome job_id=%s run_id=%s prior_status=%s "
        "resulting_status=%s delete_results=%s deletion_action=%s",
        job_id,
        reservation.run_id,
        None if reservation.prior_status is None else reservation.prior_status.value,
        (
            None
            if reservation.action is DeletionReservationAction.missing
            else "deleting"
        ),
        delete_results,
        reservation.action.value,
    )
    if reservation.action is DeletionReservationAction.missing:
        return
    if reservation.action is DeletionReservationAction.active_conflict:
        raise JobLifecycleRequestError(
            409,
            "Queued or running jobs cannot be deleted; cancel the job first.",
        )
    if reservation.action is DeletionReservationAction.policy_conflict:
        raise JobLifecycleRequestError(
            409,
            "Deletion is already reserved with a different delete_results policy; "
            "retry with the original policy.",
        )
    if reservation.action is DeletionReservationAction.pending_webhook_conflict:
        logger.info(
            "Deletion rejected by pending webhook job_id=%s run_id=%s "
            "deletion_phase=reservation recovery_action=wait_for_terminal_delivery",
            job_id,
            reservation.run_id,
        )
        raise JobLifecycleRequestError(
            409,
            "A webhook delivery is pending or in flight; retry deletion after it is terminal.",
        )

    scheduler.remove_job(job_id)
    if reservation.run_id is not None:
        try:
            queue_state = await worker_pool.inspect_delivery(reservation.run_id)
        except Exception as exc:
            logger.error(
                "Deletion Redis inspection failed job_id=%s run_id=%s "
                "deletion_phase=queue_quiescence error_type=%s",
                job_id,
                reservation.run_id,
                type(exc).__name__,
            )
            raise JobLifecycleRequestError(
                503,
                "Deletion is reserved, but Redis quiescence could not be verified; "
                "retry with the same delete_results policy.",
            ) from exc
        if queue_state in _ACTIVE_QUEUE_STATES:
            logger.error(
                "Deletion found active Redis delivery job_id=%s run_id=%s "
                "queue_outcome=%s deletion_phase=queue_quiescence",
                job_id,
                reservation.run_id,
                queue_state.value,
            )
            raise JobLifecycleRequestError(
                503,
                "Deletion is reserved, but the current Redis delivery is still active; "
                "retry with the same delete_results policy after it is quiescent.",
            )

    await _cleanup_deletion_phase(
        "errors",
        job_id,
        error_store.delete_errors_for_job(job_id),
    )
    if delete_results:
        await _cleanup_deletion_phase(
            "results",
            job_id,
            result_store.delete_results(
                job_id,
                owned_run_ids=reservation.owned_run_ids,
            ),
        )

    try:
        finalized = await job_store.finalize_job_deletion(
            job_id,
            delete_results=delete_results,
        )
    except Exception as exc:
        logger.error(
            "Final jobs.db deletion failed job_id=%s deletion_phase=jobs_db_final "
            "error_type=%s recovery_action=resume_same_policy",
            job_id,
            type(exc).__name__,
        )
        raise JobLifecycleRequestError(
            500,
            "Cross-database cleanup completed, but final job deletion failed; "
            "retry with the same delete_results policy.",
        ) from exc

    if finalized.action is DeletionFinalizationAction.pending_webhook_conflict:
        raise JobLifecycleRequestError(
            409,
            "A webhook delivery became pending during deletion; retry after it is terminal.",
        )
    if finalized.action is DeletionFinalizationAction.policy_conflict:
        raise JobLifecycleRequestError(
            409,
            "The persisted deletion policy does not match this request.",
        )
    if finalized.action is DeletionFinalizationAction.not_reserved:
        raise JobLifecycleRequestError(
            409,
            "The job no longer owns a deletion reservation; retry the request.",
        )
    logger.info(
        "Job deletion completed job_id=%s delete_results=%s "
        "deletion_phase=complete deletion_action=%s",
        job_id,
        delete_results,
        finalized.action.value,
    )


async def _cleanup_deletion_phase(
    phase: str,
    job_id: str,
    operation: Awaitable[None],
) -> None:
    """Await one cleanup coroutine and retain the deletion reservation on fault."""

    try:
        await operation
    except Exception as exc:
        logger.error(
            "Cross-database deletion cleanup failed job_id=%s deletion_phase=%s "
            "error_type=%s recovery_action=resume_same_policy",
            job_id,
            phase,
            type(exc).__name__,
        )
        raise JobLifecycleRequestError(
            500,
            f"Deletion is reserved, but {phase} cleanup failed; "
            "retry with the same delete_results policy.",
        ) from exc
    logger.info(
        "Cross-database deletion cleanup succeeded job_id=%s deletion_phase=%s",
        job_id,
        phase,
    )
