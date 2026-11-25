"""Budget exhaustion, cancellation, ownership-loss, and crash recovery for scrape tasks."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from scrapeyard.common.async_tools import shield
from scrapeyard.common.budgets import BudgetExceeded
from scrapeyard.common.time import utc_now
from scrapeyard.config.schema import GroupBy
from scrapeyard.engine.url_guard import redact_userinfo_in_text
from scrapeyard.models.job import ActionTaken, BudgetErrorDetails, ErrorType, JobStatus
from scrapeyard.queue.error_records import build_error_record
from scrapeyard.queue.heartbeat import RunHeartbeat, RunHeartbeatLeaseLost
from scrapeyard.queue.run_lifecycle import dispatch_webhook, handle_crash, save_run_result
from scrapeyard.queue.worker_context import JobExecutionContext
from scrapeyard.queue.worker_output import annotate_run_output
from scrapeyard.storage.protocols import ErrorStore, JobStore, ResultStore
from scrapeyard.storage.types import RunOwnershipError, SaveResultMeta
from scrapeyard.storage.webhook_outbox import WebhookDeliveryCreate
from scrapeyard.webhook.dispatcher import WebhookNotifier
from scrapeyard.webhook.payload import build_terminal_webhook_delivery

logger = logging.getLogger(__name__)

# Only the owning worker's finalize_owned_run writes these run statuses.
_OWNER_FINALIZED_STATUSES = frozenset({JobStatus.complete, JobStatus.partial})


def exception_detail(exc: Exception) -> str:
    detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    return redact_userinfo_in_text(detail)


def heartbeat_cutoff(
    context: JobExecutionContext | None,
    *,
    now_fn: Callable[[], datetime] = utc_now,
) -> datetime | None:
    if context is None:
        return None
    return now_fn() - timedelta(
        seconds=context.settings.workers_running_heartbeat_timeout_seconds
    )


async def error_count_snapshot(
    error_store: ErrorStore,
    *,
    job_id: str,
    run_id: str | None,
) -> int:
    if run_id is None:
        return 0
    try:
        return await error_store.count_errors_for_run(run_id)
    except Exception as exc:
        logger.warning(
            "Terminal error-count snapshot unavailable "
            "job_id=%s run_id=%s terminal_status=%s "
            "recovery_action=use_zero_error_count error_type=%s",
            job_id,
            run_id,
            JobStatus.failed.value,
            type(exc).__name__,
        )
        return 0


def failed_webhook_delivery(
    *,
    context: JobExecutionContext | None,
    job_id: str,
    run_id: str | None,
    error_count: int,
    failed_at: datetime,
) -> WebhookDeliveryCreate | None:
    if context is None or run_id is None:
        return None
    return build_terminal_webhook_delivery(
        config=context.config,
        config_hash=context.config_hash,
        job_id=job_id,
        status=JobStatus.failed,
        run_id=run_id,
        result_path=None,
        result_count=0,
        error_count=error_count,
        started_at=context.started_at,
        completed_at=failed_at,
    )


async def run_finalized_by_owner(
    job_store: JobStore,
    job_id: str,
    run_id: str,
    context: JobExecutionContext | None,
) -> bool:
    """Whether this run's own finalization committed despite the caller's error."""
    try:
        run = await job_store.get_job_run(job_id, run_id)
    except Exception as exc:
        logger.warning(
            "Run status unavailable before discarding result artifact "
            "job_id=%s run_id=%s error_type=%s",
            job_id,
            run_id,
            type(exc).__name__,
        )
        return False
    if run is None:
        return False
    owner_completed_at = None if context is None else context.finalization.completed_at
    return run.status in _OWNER_FINALIZED_STATUSES or (
        owner_completed_at is not None and run.completed_at == owner_completed_at
    )


async def discard_unowned_result(
    result_store: ResultStore,
    job_id: str,
    run_id: str | None,
    *,
    job_store: JobStore,
    context: JobExecutionContext | None,
) -> None:
    if run_id is None:
        return
    if await run_finalized_by_owner(job_store, job_id, run_id, context):
        logger.warning(
            "Kept result artifact of a run finalized before the worker stopped "
            "job_id=%s run_id=%s",
            job_id,
            run_id,
        )
        return
    try:
        deleted = await result_store.delete_result(job_id, run_id)
    except Exception as exc:
        logger.error(
            "Failed to discard unowned result artifact job_id=%s run_id=%s "
            "error_type=%s",
            job_id,
            run_id,
            type(exc).__name__,
        )
    else:
        if deleted:
            logger.warning(
                "Discarded result artifact after ownership loss job_id=%s run_id=%s",
                job_id,
                run_id,
            )


async def handle_execution_crash(
    *,
    context: JobExecutionContext | None,
    job_id: str,
    run_id: str | None,
    job_store: JobStore,
    error_store: ErrorStore,
    webhook_dispatcher: WebhookNotifier | None,
    now_fn: Callable[[], datetime] = utc_now,
) -> None:
    failed_at = now_fn()
    error_count = await error_count_snapshot(
        error_store,
        job_id=job_id,
        run_id=run_id,
    )
    delivery = failed_webhook_delivery(
        context=context,
        job_id=job_id,
        run_id=run_id,
        error_count=error_count,
        failed_at=failed_at,
    )
    committed = await handle_crash(
        job_id,
        run_id,
        job_store,
        failed_at=failed_at,
        heartbeat_cutoff=heartbeat_cutoff(context, now_fn=now_fn),
        error_count=error_count,
        webhook_delivery=delivery,
    )
    if committed and context is not None:
        await dispatch_webhook(
            webhook_dispatcher=webhook_dispatcher,
            config=context.config,
            delivery=delivery,
        )


async def handle_budget_stop(
    *,
    job_id: str,
    run_id: str | None,
    context: JobExecutionContext | None,
    heartbeat: RunHeartbeat | None,
    job_store: JobStore,
    result_store: ResultStore,
    error_store: ErrorStore,
    webhook_dispatcher: WebhookNotifier | None,
    error: BudgetExceeded,
    now_fn: Callable[[], datetime] = utc_now,
) -> None:
    """Fail a run that hit a hard run limit, falling back to crash handling."""
    if context is None:
        logger.error(
            "Run budget exceeded before context load job_id=%s run_id=%s "
            "limit_name=%s configured_limit=%s observed_amount=%s",
            job_id,
            run_id,
            error.limit_name.value,
            error.configured_limit,
            error.observed_amount,
        )
        await handle_execution_crash(
            context=None,
            job_id=job_id,
            run_id=run_id,
            job_store=job_store,
            error_store=error_store,
            webhook_dispatcher=webhook_dispatcher,
            now_fn=now_fn,
        )
        return
    try:
        await handle_budget_exhaustion(
            context=context,
            job_id=job_id,
            run_id=context.run_id,
            error=error,
            job_store=job_store,
            result_store=result_store,
            error_store=error_store,
            webhook_dispatcher=webhook_dispatcher,
            heartbeat=heartbeat,
            now_fn=now_fn,
        )
    except Exception as handler_exc:
        if heartbeat is not None:
            await heartbeat.stop()
        logger.error(
            "Budget failure handling crashed job_id=%s run_id=%s error_type=%s",
            job_id,
            context.run_id,
            type(handler_exc).__name__,
        )
        await discard_unowned_result(
            result_store, job_id, context.run_id, job_store=job_store, context=context
        )
        await handle_execution_crash(
            context=context,
            job_id=job_id,
            run_id=context.run_id,
            job_store=job_store,
            error_store=error_store,
            webhook_dispatcher=webhook_dispatcher,
            now_fn=now_fn,
        )


async def handle_worker_cancellation(
    *,
    job_id: str,
    run_id: str | None,
    context: JobExecutionContext | None,
    heartbeat: RunHeartbeat | None,
    job_store: JobStore,
    result_store: ResultStore,
    error_store: ErrorStore,
    webhook_dispatcher: WebhookNotifier | None,
    now_fn: Callable[[], datetime] = utc_now,
) -> bool:
    """Record the outcome of a cancelled run; return whether to propagate cancellation."""
    lease_lost = heartbeat is not None and heartbeat.ownership_lost
    if heartbeat is not None:
        await heartbeat.stop()
    if lease_lost:
        logger.error(
            "Stopping scrape after heartbeat lease loss job_id=%s run_id=%s reason=%s",
            job_id,
            run_id,
            heartbeat.lost_reason if heartbeat is not None else None,
        )
        await discard_unowned_result(
            result_store, job_id, run_id, job_store=job_store, context=context
        )
        return False
    active = False
    if run_id is not None:
        try:
            active = await job_store.run_is_active(job_id, run_id)
        except Exception as exc:
            logger.warning(
                "Cancellation ownership check unavailable job_id=%s run_id=%s "
                "cancellation_phase=worker_cancel_handler error_type=%s",
                job_id,
                run_id,
                type(exc).__name__,
            )
            active = True
    logger.warning("scrape_task cancelled for job_id=%s run_id=%s", job_id, run_id)
    await discard_unowned_result(
        result_store, job_id, run_id, job_store=job_store, context=context
    )
    if not active:
        logger.info(
            "Cooperative worker cancellation completed job_id=%s run_id=%s "
            "cancellation_checkpoint=task_cancel_handler "
            "recovery_action=skip_failed_finalization",
            job_id,
            run_id,
        )
        return True
    recovery = asyncio.create_task(
        handle_execution_crash(
            context=context,
            job_id=job_id,
            run_id=run_id,
            job_store=job_store,
            error_store=error_store,
            webhook_dispatcher=webhook_dispatcher,
            now_fn=now_fn,
        )
    )
    try:
        await shield(recovery)
    except asyncio.CancelledError:
        await recovery
    return True


async def handle_budget_exhaustion(
    *,
    context: JobExecutionContext,
    job_id: str,
    run_id: str,
    error: BudgetExceeded,
    job_store: JobStore,
    result_store: ResultStore,
    error_store: ErrorStore,
    webhook_dispatcher: WebhookNotifier | None,
    heartbeat: RunHeartbeat | None,
    now_fn: Callable[[], datetime] = utc_now,
) -> None:
    """Persist a controlled, queryable failed outcome for any hard run limit."""
    try:
        await context.activity.checkpoint("before_budget_failure_persistence")
    except RunOwnershipError:
        await discard_unowned_result(
            result_store, job_id, run_id, job_store=job_store, context=context
        )
        return
    details = BudgetErrorDetails(
        limit_name=error.limit_name.value,
        configured_limit=error.configured_limit,
        observed_amount=error.observed_amount,
    )
    message = str(error)
    logger.error(
        "Run budget exceeded job_id=%s run_id=%s limit_name=%s "
        "configured_limit=%s observed_amount=%s",
        job_id,
        run_id,
        error.limit_name.value,
        error.configured_limit,
        error.observed_amount,
    )
    budget_record = build_error_record(
        job_id,
        run_id,
        context.config.project,
        "",
        0,
        ErrorType.budget_exceeded,
        None,
        "service_budget",
        ActionTaken.fail,
        error_message=message,
        budget=details,
    )
    try:
        await context.activity.checkpoint("before_budget_error_flush")
        await error_store.log_error(budget_record)
    except Exception as exc:
        logger.error(
            "Failed to persist budget error job_id=%s run_id=%s error_type=%s",
            job_id,
            run_id,
            type(exc).__name__,
        )

    completed_at = now_fn()
    output_data: dict[str, Any] = {
        "project": context.config.project,
        "name": context.config.name,
        "job_id": job_id,
        "status": JobStatus.failed.value,
        "completed_at": completed_at.isoformat(),
        "errors": [message],
        "targets": [],
        "budget_error": details.model_dump(mode="json"),
        "run_budget": context.budget.snapshot(),
        "results": [] if context.config.output.group_by == GroupBy.merge else {},
    }
    annotate_run_output(output_data, context)
    save_meta: SaveResultMeta | None = None
    try:
        await context.activity.checkpoint("before_budget_result_persistence")
        save_meta = await save_run_result(
            job_id=job_id,
            run_id=run_id,
            config_hash=context.config_hash,
            result_store=result_store,
            output_data=output_data,
            final_status=JobStatus.failed,
            record_count=0,
            max_serialized_bytes=context.budget.max_serialized_result_bytes,
        )
        await context.activity.checkpoint("after_budget_result_persistence")
    except RunOwnershipError:
        await discard_unowned_result(
            result_store, job_id, run_id, job_store=job_store, context=context
        )
        return
    except Exception as exc:
        logger.error(
            "Failed to persist terminal budget result job_id=%s run_id=%s "
            "error_type=%s",
            job_id,
            run_id,
            type(exc).__name__,
        )

    if heartbeat is not None:
        try:
            heartbeat.ensure_owned()
        except RunHeartbeatLeaseLost:
            await heartbeat.stop()
            await discard_unowned_result(
                result_store, job_id, run_id, job_store=job_store, context=context
            )
            return
        await heartbeat.stop()

    error_count = await error_count_snapshot(
        error_store,
        job_id=job_id,
        run_id=run_id,
    )
    webhook_delivery = build_terminal_webhook_delivery(
        config=context.config,
        config_hash=context.config_hash,
        job_id=job_id,
        status=JobStatus.failed,
        run_id=run_id,
        result_path=save_meta.file_path if save_meta is not None else None,
        result_count=save_meta.record_count if save_meta is not None else 0,
        error_count=error_count,
        started_at=context.started_at,
        completed_at=completed_at,
    )
    try:
        context.finalization.completed_at = completed_at
        await job_store.finalize_owned_run(
            job_id,
            run_id,
            JobStatus.failed.value,
            0,
            error_count,
            completed_at,
            completed_at
            - timedelta(
                seconds=context.settings.workers_running_heartbeat_timeout_seconds
            ),
            webhook_delivery=webhook_delivery,
        )
    except RunOwnershipError:
        logger.info(
            "Budget finalization rejected job_id=%s run_id=%s ownership_outcome=lost",
            job_id,
            run_id,
        )
        await discard_unowned_result(
            result_store, job_id, run_id, job_store=job_store, context=context
        )
        return
    except Exception as exc:
        logger.error(
            "Failed to finalize terminal budget status job_id=%s run_id=%s "
            "error_type=%s",
            job_id,
            run_id,
            type(exc).__name__,
        )
        await discard_unowned_result(
            result_store, job_id, run_id, job_store=job_store, context=context
        )
        await handle_execution_crash(
            context=context,
            job_id=job_id,
            run_id=run_id,
            job_store=job_store,
            error_store=error_store,
            webhook_dispatcher=webhook_dispatcher,
            now_fn=now_fn,
        )
        return

    await dispatch_webhook(
        webhook_dispatcher=webhook_dispatcher,
        config=context.config,
        delivery=webhook_delivery,
    )
