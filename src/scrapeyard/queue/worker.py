"""Stateless scrape task: orchestrates fetch → validate → store."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from scrapeyard.common.budgets import BudgetExceeded, RunBudget
from scrapeyard.common.ids import generate_run_id
from scrapeyard.common.qualification import qualification_checkpoint
from scrapeyard.common.settings import ServiceSettings, get_settings
from scrapeyard.common.time import utc_now
from scrapeyard.config.loader import load_config
from scrapeyard.config.schema import FailStrategy, FetcherType, GroupBy, ScrapeConfig, TargetConfig
from scrapeyard.engine.fetch_classifier import classify_fetch_exception
from scrapeyard.engine.rate_limiter import DomainRateLimiter
from scrapeyard.engine.resilience import CircuitBreaker, ResultValidator
from scrapeyard.engine.scraper import TargetResult, TargetStatus, scrape_target
from scrapeyard.engine.url_guard import (
    activate_deployment_secret_redaction,
    redact_sensitive_mapping,
    redact_userinfo_in_text,
    redact_userinfo_in_url,
    reset_deployment_secret_redaction,
    url_host_label,
)
from scrapeyard.models.job import (
    ActionTaken,
    BudgetErrorDetails,
    ErrorRecord,
    ErrorType,
    Job,
    JobStatus,
)
from scrapeyard.queue.browser_limiter import BrowserExecutionLimiter
from scrapeyard.queue.cancellation import RunActivityGuard
from scrapeyard.queue.error_records import TargetErrorRecorder, build_error_record
from scrapeyard.queue.heartbeat import RunHeartbeat, RunHeartbeatLeaseLost
from scrapeyard.queue.run_lifecycle import (
    build_run_paths,
    dispatch_webhook,
    handle_crash,
    save_run_result,
)
from scrapeyard.queue.target_execution import (
    TargetRuntimeContext,
    guard_target_execution,
    log_target_fetch,
    record_failed_target,
    resolve_target_runtime_context,
)
from scrapeyard.queue.validation_policy import apply_validation
from scrapeyard.runtime.metrics import (
    OUTPUT_BYTES,
    active_target,
    observe_run,
    observe_target,
)
from scrapeyard.storage.protocols import ErrorStore, JobStore, ResultStore
from scrapeyard.storage.types import RunOwnershipError, SaveResultMeta
from scrapeyard.storage.webhook_outbox import WebhookDeliveryCreate
from scrapeyard.webhook.dispatcher import WebhookNotifier
from scrapeyard.webhook.payload import build_terminal_webhook_delivery

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobExecutionContext:
    config: ScrapeConfig
    job: Job
    settings: ServiceSettings
    started_at: datetime
    adaptive_dir: str
    run_artifacts_dir: str | None
    budget: RunBudget
    run_id: str
    activity: RunActivityGuard


@dataclass(frozen=True)
class PersistedJobResult:
    final_status: JobStatus
    flat_data: list[dict[str, Any]]
    all_errors: list[str]
    save_meta: SaveResultMeta


@dataclass(frozen=True)
class TargetProcessingContext:
    config: ScrapeConfig
    job_id: str
    run_id: str | None
    settings: ServiceSettings
    adaptive_dir: str
    run_artifacts_dir: str | None
    circuit_breaker: CircuitBreaker
    rate_limiter: DomainRateLimiter
    validator: ResultValidator
    browser_limiter: BrowserExecutionLimiter | None
    budget: RunBudget
    activity: RunActivityGuard

    def recorder(self, pending_errors: list[ErrorRecord]) -> TargetErrorRecorder:
        return TargetErrorRecorder(
            job_id=self.job_id,
            run_id=self.run_id,
            project=self.config.project,
            pending_errors=pending_errors,
            circuit_breaker=self.circuit_breaker,
        )


async def scrape_task(
    job_id: str,
    config_yaml: str,
    *,
    run_id: str | None = None,
    trigger: str = "adhoc",
    job_store: JobStore,
    result_store: ResultStore,
    error_store: ErrorStore,
    circuit_breaker: CircuitBreaker,
    rate_limiter: DomainRateLimiter,
    browser_limiter: BrowserExecutionLimiter | None = None,
    webhook_dispatcher: WebhookNotifier | None = None,
) -> None:
    """Execute a complete scrape job."""
    context: JobExecutionContext | None = None
    heartbeat: RunHeartbeat | None = None
    active_run_id = run_id
    metric_claimed = False
    metric_status = "ignored"
    metric_started = time.monotonic()
    redaction_token = None
    try:
        context = await _load_job_execution_context(job_id, config_yaml, run_id, job_store)
        if context is None:
            return
        redaction_token = activate_deployment_secret_redaction(
            context.config.resolved_secret_values
        )
        active_run_id = context.run_id

        claimed = await _mark_run_started(
            context,
            job_id,
            context.run_id,
            trigger,
            config_yaml,
            job_store,
        )
        if not claimed:
            return
        metric_claimed = True
        qualification_checkpoint("after_claim_run_creation")
        heartbeat = RunHeartbeat(
            job_store=job_store,
            job_id=job_id,
            run_id=context.run_id,
            last_success_at=context.started_at,
            interval_seconds=context.settings.workers_heartbeat_interval_seconds,
            timeout_seconds=context.settings.workers_running_heartbeat_timeout_seconds,
        )
        heartbeat.start()
        await context.activity.checkpoint("before_targets")
        all_results = await _process_all_targets(
            context=context,
            job_id=job_id,
            run_id=context.run_id,
            circuit_breaker=circuit_breaker,
            rate_limiter=rate_limiter,
            browser_limiter=browser_limiter,
            error_store=error_store,
        )
        persisted = await _persist_job_results(
            context=context,
            job_id=job_id,
            run_id=context.run_id,
            all_results=all_results,
            result_store=result_store,
        )
        heartbeat.ensure_owned()
        await heartbeat.stop()
        await _finalize_job_execution(
            context=context,
            job_id=job_id,
            run_id=context.run_id,
            persisted=persisted,
            job_store=job_store,
            result_store=result_store,
            error_store=error_store,
            webhook_dispatcher=webhook_dispatcher,
        )
        metric_status = persisted.final_status.value
    except BudgetExceeded as exc:
        metric_status = "failed"
        if context is None:
            logger.error(
                "Run budget exceeded before context load job_id=%s run_id=%s "
                "limit_name=%s configured_limit=%s observed_amount=%s",
                job_id,
                active_run_id,
                exc.limit_name.value,
                exc.configured_limit,
                exc.observed_amount,
            )
            await _handle_execution_crash(
                context=None,
                job_id=job_id,
                run_id=active_run_id,
                job_store=job_store,
                error_store=error_store,
                webhook_dispatcher=webhook_dispatcher,
            )
            return
        await _handle_budget_exhaustion(
            context=context,
            job_id=job_id,
            run_id=context.run_id,
            error=exc,
            job_store=job_store,
            result_store=result_store,
            error_store=error_store,
            webhook_dispatcher=webhook_dispatcher,
            heartbeat=heartbeat,
        )
    except asyncio.CancelledError:
        metric_status = "cancelled"
        lease_lost = heartbeat is not None and heartbeat.ownership_lost
        if heartbeat is not None:
            await heartbeat.stop()
        if lease_lost:
            logger.error(
                "Stopping scrape after heartbeat lease loss job_id=%s run_id=%s reason=%s",
                job_id,
                active_run_id,
                heartbeat.lost_reason if heartbeat is not None else None,
            )
            await _discard_unowned_result(result_store, job_id, active_run_id)
            return
        active = False
        if active_run_id is not None:
            try:
                active = await job_store.run_is_active(job_id, active_run_id)
            except Exception as exc:
                logger.warning(
                    "Cancellation ownership check unavailable job_id=%s run_id=%s "
                    "cancellation_phase=worker_cancel_handler error_type=%s",
                    job_id,
                    active_run_id,
                    type(exc).__name__,
                )
                active = True
        logger.warning("scrape_task cancelled for job_id=%s run_id=%s", job_id, active_run_id)
        await _discard_unowned_result(result_store, job_id, active_run_id)
        if not active:
            logger.info(
                "Cooperative worker cancellation completed job_id=%s run_id=%s "
                "cancellation_checkpoint=task_cancel_handler "
                "recovery_action=skip_failed_finalization",
                job_id,
                active_run_id,
            )
            raise
        recovery = asyncio.create_task(
            _handle_execution_crash(
                context=context,
                job_id=job_id,
                run_id=active_run_id,
                job_store=job_store,
                error_store=error_store,
                webhook_dispatcher=webhook_dispatcher,
            )
        )
        try:
            await asyncio.shield(recovery)
        except asyncio.CancelledError:
            await recovery
        raise
    except (RunHeartbeatLeaseLost, RunOwnershipError) as exc:
        metric_status = "ignored"
        if heartbeat is not None:
            await heartbeat.stop()
        logger.info(
            "Stopping stale worker job_id=%s run_id=%s ownership_outcome=lost detail=%s",
            job_id,
            active_run_id,
            exc,
        )
        await _discard_unowned_result(result_store, job_id, active_run_id)
    except Exception as exc:
        metric_status = "failed"
        if heartbeat is not None:
            await heartbeat.stop()
        logger.error(
            "scrape_task crashed for job_id=%s run_id=%s: %s",
            job_id,
            active_run_id,
            _exception_detail(exc),
        )
        await _discard_unowned_result(result_store, job_id, active_run_id)
        await _handle_execution_crash(
            context=context,
            job_id=job_id,
            run_id=active_run_id,
            job_store=job_store,
            error_store=error_store,
            webhook_dispatcher=webhook_dispatcher,
        )
    finally:
        if heartbeat is not None:
            await heartbeat.stop()
        if redaction_token is not None:
            reset_deployment_secret_redaction(redaction_token)
        if metric_claimed:
            observe_run(
                status=metric_status,
                trigger=trigger,
                duration_seconds=time.monotonic() - metric_started,
            )


def _heartbeat_cutoff(context: JobExecutionContext | None) -> datetime | None:
    if context is None:
        return None
    return utc_now() - timedelta(
        seconds=context.settings.workers_running_heartbeat_timeout_seconds
    )


async def _error_count_snapshot(
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


def _failed_webhook_delivery(
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
        job_id=job_id,
        status=JobStatus.failed,
        run_id=run_id,
        result_path=None,
        result_count=0,
        error_count=error_count,
        started_at=context.started_at,
        completed_at=failed_at,
    )


async def _handle_execution_crash(
    *,
    context: JobExecutionContext | None,
    job_id: str,
    run_id: str | None,
    job_store: JobStore,
    error_store: ErrorStore,
    webhook_dispatcher: WebhookNotifier | None,
) -> None:
    failed_at = utc_now()
    error_count = await _error_count_snapshot(
        error_store,
        job_id=job_id,
        run_id=run_id,
    )
    delivery = _failed_webhook_delivery(
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
        heartbeat_cutoff=_heartbeat_cutoff(context),
        error_count=error_count,
        webhook_delivery=delivery,
    )
    if committed and context is not None:
        await dispatch_webhook(
            webhook_dispatcher=webhook_dispatcher,
            config=context.config,
            delivery=delivery,
        )


async def _discard_unowned_result(
    result_store: ResultStore,
    job_id: str,
    run_id: str | None,
) -> None:
    if run_id is None:
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


async def _load_job_execution_context(
    job_id: str,
    config_yaml: str,
    run_id: str | None,
    job_store: JobStore,
) -> JobExecutionContext | None:
    config = await asyncio.to_thread(load_config, config_yaml)
    job = await job_store.get_job(job_id)
    effective_run_id = run_id or job.current_run_id or generate_run_id()
    settings = get_settings()
    adaptive_dir, run_artifacts_dir = build_run_paths(
        settings,
        config.project,
        job.name,
        effective_run_id,
    )
    if _should_skip_delivery(job, effective_run_id):
        logger.info(
            "Skipping duplicate, terminal, or superseded delivery "
            "job_id=%s run_id=%s current_run_id=%s status=%s",
            job_id,
            effective_run_id,
            job.current_run_id,
            job.status.value,
        )
        return None
    started_at = utc_now()
    return JobExecutionContext(
        config=config,
        job=job,
        settings=settings,
        started_at=started_at,
        adaptive_dir=adaptive_dir,
        run_artifacts_dir=run_artifacts_dir,
        budget=RunBudget.from_settings(settings),
        run_id=effective_run_id,
        activity=RunActivityGuard(
            job_store=job_store,
            job_id=job_id,
            run_id=effective_run_id,
        ),
    )


async def _mark_run_started(
    context: JobExecutionContext,
    job_id: str,
    run_id: str,
    trigger: str,
    config_yaml: str,
    job_store: JobStore,
) -> bool:
    if context.job.current_run_id is None:
        queued = await job_store.queue_run(
            job_id,
            expected_status=JobStatus.queued.value,
            expected_run_id=None,
            new_run_id=run_id,
            new_trigger=trigger,
            queued_at=context.started_at,
        )
        if not queued:
            return False
    config_hash = hashlib.sha256(config_yaml.encode()).hexdigest()
    claimed = await job_store.claim_run(
        run_id,
        job_id,
        trigger,
        config_hash,
        context.started_at,
    )
    if not claimed:
        logger.info(
            "Run claim rejected job_id=%s run_id=%s ownership_outcome=lost",
            job_id,
            run_id,
        )
        return False
    context.budget.check_deadline()
    return True


async def _persist_job_results(
    *,
    context: JobExecutionContext,
    job_id: str,
    run_id: str,
    all_results: list[TargetResult],
    result_store: ResultStore,
) -> PersistedJobResult:
    await context.activity.checkpoint("before_result_persistence")
    context.budget.check_deadline()
    flat_data, all_errors = _collect_result_payload(all_results)
    final_status = _determine_final_status(context.config, all_results, flat_data)
    publish_results = not (
        final_status == JobStatus.failed
        and context.config.execution.fail_strategy == FailStrategy.all_or_nothing
    )
    if not publish_results:
        flat_data.clear()

    output_data = redact_sensitive_mapping(
        _format_output(
            context.config,
            all_results,
            job_id,
            final_status,
            all_errors,
            publish_results=publish_results,
        ),
        secret_values=context.config.resolved_secret_values,
    )
    context.budget.check_deadline()
    save_meta = await save_run_result(
        job_id=job_id,
        run_id=run_id,
        result_store=result_store,
        output_data=output_data,
        final_status=final_status,
        record_count=len(flat_data),
        budget=context.budget,
    )
    if isinstance(save_meta.serialized_bytes, int | float):
        OUTPUT_BYTES.inc(save_meta.serialized_bytes)
    await context.activity.checkpoint("after_result_persistence")
    return PersistedJobResult(
        final_status=final_status,
        flat_data=flat_data,
        all_errors=all_errors,
        save_meta=save_meta,
    )


async def _finalize_job_execution(
    *,
    context: JobExecutionContext,
    job_id: str,
    run_id: str,
    persisted: PersistedJobResult,
    job_store: JobStore,
    result_store: ResultStore,
    error_store: ErrorStore,
    webhook_dispatcher: WebhookNotifier | None,
) -> None:
    await context.activity.checkpoint("before_terminal_finalization")
    completed_at = utc_now()
    error_count = await error_store.count_errors_for_run(run_id)
    webhook_delivery = build_terminal_webhook_delivery(
        config=context.config,
        job_id=job_id,
        status=persisted.final_status,
        run_id=run_id,
        result_path=persisted.save_meta.file_path,
        result_count=persisted.save_meta.record_count,
        error_count=error_count,
        started_at=context.started_at,
        completed_at=completed_at,
    )
    try:
        qualification_checkpoint("during_run_finalization")
        await job_store.finalize_owned_run(
            job_id,
            run_id,
            persisted.final_status.value,
            len(persisted.flat_data),
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
            "Finalization rejected job_id=%s run_id=%s ownership_outcome=lost "
            "last_heartbeat_timeout_seconds=%s",
            job_id,
            run_id,
            context.settings.workers_running_heartbeat_timeout_seconds,
        )
        raise

    qualification_checkpoint("after_terminal_state_before_delivery_ack")
    await dispatch_webhook(
        webhook_dispatcher=webhook_dispatcher,
        config=context.config,
        delivery=webhook_delivery,
    )


async def _handle_budget_exhaustion(
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
) -> None:
    """Persist a controlled, queryable failed outcome for any hard run limit."""
    try:
        await context.activity.checkpoint("before_budget_failure_persistence")
    except RunOwnershipError:
        await _discard_unowned_result(result_store, job_id, run_id)
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

    completed_at = utc_now()
    output_data: dict[str, Any] = {
        "project": context.config.project,
        "name": context.config.name,
        "job_id": job_id,
        "status": JobStatus.failed.value,
        "completed_at": completed_at.isoformat(),
        "errors": [message],
        "targets": [],
        "budget_error": details.model_dump(mode="json"),
        "results": [] if context.config.output.group_by == GroupBy.merge else {},
    }
    save_meta: SaveResultMeta | None = None
    try:
        await context.activity.checkpoint("before_budget_result_persistence")
        save_meta = await save_run_result(
            job_id=job_id,
            run_id=run_id,
            result_store=result_store,
            output_data=output_data,
            final_status=JobStatus.failed,
            record_count=0,
            max_serialized_bytes=context.budget.max_serialized_result_bytes,
        )
        await context.activity.checkpoint("after_budget_result_persistence")
    except RunOwnershipError:
        await _discard_unowned_result(result_store, job_id, run_id)
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
            await _discard_unowned_result(result_store, job_id, run_id)
            return
        await heartbeat.stop()

    error_count = await _error_count_snapshot(
        error_store,
        job_id=job_id,
        run_id=run_id,
    )
    webhook_delivery = build_terminal_webhook_delivery(
        config=context.config,
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
        await _discard_unowned_result(result_store, job_id, run_id)
        return
    except Exception as exc:
        logger.error(
            "Failed to finalize terminal budget status job_id=%s run_id=%s "
            "error_type=%s",
            job_id,
            run_id,
            type(exc).__name__,
        )
        await _discard_unowned_result(result_store, job_id, run_id)
        await _handle_execution_crash(
            context=context,
            job_id=job_id,
            run_id=run_id,
            job_store=job_store,
            error_store=error_store,
            webhook_dispatcher=webhook_dispatcher,
        )
        return

    await dispatch_webhook(
        webhook_dispatcher=webhook_dispatcher,
        config=context.config,
        delivery=webhook_delivery,
    )


async def _process_all_targets(
    *,
    context: JobExecutionContext,
    job_id: str,
    run_id: str | None,
    circuit_breaker: CircuitBreaker,
    rate_limiter: DomainRateLimiter,
    browser_limiter: BrowserExecutionLimiter | None,
    error_store: ErrorStore,
) -> list[TargetResult]:
    """Dispatch all targets with concurrency, delay, and rate limiting."""
    config = context.config
    targets = list(config.resolved_targets())
    sem = asyncio.Semaphore(config.execution.concurrency)
    start_lock = asyncio.Lock()
    next_start_at = 0.0
    target_context = TargetProcessingContext(
        config=config,
        job_id=job_id,
        run_id=run_id,
        settings=context.settings,
        adaptive_dir=context.adaptive_dir,
        run_artifacts_dir=context.run_artifacts_dir,
        circuit_breaker=circuit_breaker,
        rate_limiter=rate_limiter,
        validator=ResultValidator(config.validation),
        browser_limiter=browser_limiter,
        budget=context.budget,
        activity=context.activity,
    )

    async def _process_one(target_cfg: TargetConfig) -> TargetResult:
        nonlocal next_start_at
        pending_errors: list[ErrorRecord] = []
        cancelled = False
        target_result: TargetResult | None = None
        target_started = 0.0
        try:
            await context.activity.checkpoint("before_target_start")
            async with sem:
                await context.activity.checkpoint("before_target_fetch")
                if config.execution.delay_between > 0:
                    async with start_lock:
                        wait_seconds = max(0.0, next_start_at - time.monotonic())
                        if wait_seconds > 0:
                            await context.budget.sleep(wait_seconds)
                            await context.activity.checkpoint("after_target_delay")
                        context.budget.check_deadline()
                        next_start_at = (
                            time.monotonic() + config.execution.delay_between
                        )
                context.budget.check_deadline()
                target_started = time.monotonic()
                with active_target():
                    target_result = await _fetch_and_validate_target(
                        target_cfg=target_cfg,
                        context=target_context,
                        pending_errors=pending_errors,
                    )
                return target_result
        except asyncio.CancelledError:
            cancelled = True
            pending_errors.clear()
            raise
        finally:
            if target_started:
                observe_target(
                    status=(
                        "cancelled"
                        if cancelled
                        else (
                            "failed"
                            if target_result is None
                            else target_result.status_value
                        )
                    ),
                    fetcher=target_cfg.fetcher.value,
                    duration_seconds=time.monotonic() - target_started,
                    records=0 if target_result is None else len(target_result.data),
                )
            if not cancelled:
                await _flush_errors(
                    error_store,
                    pending_errors,
                    activity=context.activity,
                )

    tasks = [
        asyncio.create_task(_process_one(target_cfg))
        for target_cfg in targets
    ]
    try:
        outcomes = await context.budget.wait_for(asyncio.gather(*tasks))
        return list(outcomes)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


async def _fetch_and_validate_target(
    *,
    target_cfg: TargetConfig,
    context: TargetProcessingContext,
    pending_errors: list[ErrorRecord],
) -> TargetResult:
    """Fetch a single target and run validation. Returns the result."""
    recorder = context.recorder(pending_errors)
    runtime = resolve_target_runtime_context(
        target_cfg=target_cfg,
        config=context.config,
        settings=context.settings,
        run_artifacts_dir=context.run_artifacts_dir,
    )
    circuit_open = await guard_target_execution(
        runtime=runtime,
        config=context.config,
        target_cfg=target_cfg,
        circuit_breaker=context.circuit_breaker,
        rate_limiter=context.rate_limiter,
        recorder=recorder,
        budget=context.budget,
        cancellation_guard=context.activity.checkpoint,
    )
    if circuit_open is not None:
        return TargetResult(url=target_cfg.url, status=TargetStatus.failed, errors=[str(circuit_open)])

    log_target_fetch(target_cfg, runtime)
    try:
        qualification_checkpoint("during_target_execution")
        if target_cfg.fetcher in (FetcherType.dynamic, FetcherType.stealthy):
            if context.browser_limiter is None:
                return await _scrape_and_validate_target(target_cfg, context, runtime, recorder)
            async with context.browser_limiter.slot():
                return await _scrape_and_validate_target(target_cfg, context, runtime, recorder)
        return await _scrape_and_validate_target(target_cfg, context, runtime, recorder)
    except asyncio.CancelledError:
        context.circuit_breaker.abort_probe(runtime.domain, runtime.circuit_probe)
        runtime.circuit_probe = None
        raise
    except RunOwnershipError:
        context.circuit_breaker.abort_probe(runtime.domain, runtime.circuit_probe)
        runtime.circuit_probe = None
        raise
    except BudgetExceeded:
        recorder.record_success(runtime.domain, probe=runtime.circuit_probe)
        runtime.circuit_probe = None
        raise
    except Exception as exc:
        return _target_exception_result(
            runtime=runtime,
            target_cfg=target_cfg,
            recorder=recorder,
            exc=exc,
        )


async def _scrape_and_validate_target(
    target_cfg: TargetConfig,
    context: TargetProcessingContext,
    runtime: TargetRuntimeContext,
    recorder: TargetErrorRecorder,
) -> TargetResult:
    result = await scrape_target(
        target_cfg,
        runtime.adaptive,
        context.config.retry,
        adaptive_dir=context.adaptive_dir,
        proxy_url=runtime.proxy_url,
        artifacts_dir=runtime.artifacts_dir,
        budget=context.budget,
        cancellation_guard=context.activity.checkpoint,
    )
    await context.activity.checkpoint("after_target_fetch")
    if not result.is_success:
        record_failed_target(
            runtime=runtime,
            result=result,
            target_cfg=target_cfg,
            recorder=recorder,
        )
        return result

    recorder.record_success(runtime.domain, probe=runtime.circuit_probe)
    runtime.circuit_probe = None
    return await apply_validation(
        target_cfg=target_cfg,
        domain=runtime.domain,
        adaptive=runtime.adaptive,
        result=result,
        config=context.config,
        adaptive_dir=context.adaptive_dir,
        run_artifacts_dir=context.run_artifacts_dir,
        recorder=recorder,
        rate_limiter=context.rate_limiter,
        validator=context.validator,
        scrape=scrape_target,
        proxy_url=runtime.proxy_url,
        budget=context.budget,
        cancellation_guard=context.activity.checkpoint,
    )


def _exception_detail(exc: Exception) -> str:
    detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    return redact_userinfo_in_text(detail)


def _target_exception_result(
    *,
    runtime: TargetRuntimeContext,
    target_cfg: TargetConfig,
    recorder: TargetErrorRecorder,
    exc: Exception,
) -> TargetResult:
    error_type, http_status, debug = classify_fetch_exception(exc, target_cfg.fetcher)
    detail = _exception_detail(exc)
    logger.warning(
        "Target processing failed for job_id=%s run_id=%s url=%s: %s",
        recorder.job_id,
        recorder.run_id,
        redact_userinfo_in_url(target_cfg.url),
        detail,
    )
    result = TargetResult(
        url=target_cfg.url,
        status=TargetStatus.failed,
        data=[],
        errors=[detail],
        pages_scraped=0,
        error_type=error_type,
        http_status=http_status,
        error_detail=detail,
        debug=debug,
    )
    record_failed_target(
        runtime=runtime,
        result=result,
        target_cfg=target_cfg,
        recorder=recorder,
    )
    return result


def _collect_result_payload(all_results: list[TargetResult]) -> tuple[list[dict[str, Any]], list[str]]:
    flat_data: list[dict[str, Any]] = []
    all_errors: list[str] = []
    for target_result in all_results:
        flat_data.extend(target_result.data)
        all_errors.extend(target_result.errors)
    return flat_data, all_errors


def _target_result_details(
    result: TargetResult,
    *,
    records_accepted: bool = True,
) -> dict[str, Any]:
    debug = redact_sensitive_mapping(result.debug) if result.debug is not None else None
    if isinstance(debug, dict):
        debug["screenshot_path"] = None
    return {
        "status": result.status_value,
        "count": len(result.data) if records_accepted else 0,
        "observed_count": len(result.data),
        "debug": debug,
        "error_type": result.error_type.value if result.error_type else None,
        "error_detail": (
            redact_userinfo_in_text(result.error_detail)
            if result.error_detail is not None
            else None
        ),
    }


def _unique_result_group_key(grouped: dict[str, Any], url: str) -> str:
    base_key = url_host_label(url)
    if base_key not in grouped:
        return base_key
    index = 2
    while f"{base_key}#{index}" in grouped:
        index += 1
    return f"{base_key}#{index}"


def _determine_final_status(
    config: ScrapeConfig,
    all_results: list[TargetResult],
    flat_data: list[dict[str, Any]],
) -> JobStatus:
    """Determine the final job status based on results and fail_strategy."""
    failed_count = sum(1 for result in all_results if result.is_failed)
    fail_strategy = config.execution.fail_strategy

    if fail_strategy == FailStrategy.all_or_nothing:
        if failed_count > 0:
            return JobStatus.failed
        return JobStatus.complete
    if fail_strategy == FailStrategy.continue_:
        return JobStatus.complete if flat_data else JobStatus.failed
    if failed_count == len(all_results) or not flat_data:
        return JobStatus.failed
    if failed_count > 0:
        return JobStatus.partial
    return JobStatus.complete


def _format_output(
    config: ScrapeConfig,
    all_results: list[TargetResult],
    job_id: str,
    final_status: JobStatus,
    all_errors: list[str],
    *,
    publish_results: bool = True,
) -> dict[str, Any]:
    """Build the output data dict for result storage."""
    redacted_errors = [redact_userinfo_in_text(error) for error in all_errors]
    job_meta: dict[str, Any] = {
        "project": config.project,
        "name": config.name,
        "job_id": job_id,
        "status": final_status.value,
        "completed_at": utc_now().isoformat(),
        "errors": redacted_errors,
        "targets": [
            {
                "url": redact_userinfo_in_url(result.url),
                **_target_result_details(
                    result,
                    records_accepted=publish_results,
                ),
                "pages_scraped": result.pages_scraped,
                "errors": [redact_userinfo_in_text(error) for error in result.errors],
            }
            for result in all_results
        ],
    }

    if not publish_results:
        empty_results: list[Any] | dict[str, Any]
        empty_results = [] if config.output.group_by == GroupBy.merge else {}
        return {**job_meta, "results": empty_results}

    if config.output.group_by == GroupBy.merge:
        merged: list[Any] = []
        for result in all_results:
            source = url_host_label(result.url)
            for item in result.data:
                if isinstance(item, dict):
                    merged.append({**item, "_source": source})
                else:
                    merged.append(item)
        return {**job_meta, "results": merged}

    grouped: dict[str, Any] = {}
    for result in all_results:
        group_key = _unique_result_group_key(grouped, result.url)
        grouped[group_key] = {
            **_target_result_details(result),
            "data": result.data,
        }
    return {**job_meta, "results": grouped}


def _run_superseded(job: Job, run_id: str | None) -> bool:
    return (
        run_id is not None
        and job.current_run_id is not None
        and job.current_run_id != run_id
    )


def _should_skip_delivery(
    job: Job,
    run_id: str,
) -> bool:
    if _run_superseded(job, run_id):
        return True
    if job.status in {
        JobStatus.complete,
        JobStatus.partial,
        JobStatus.failed,
        JobStatus.cancelled,
        JobStatus.deleting,
    }:
        return True
    # A run can only be claimed once. Duplicate delivery of a running run must
    # wait for conditional recovery to create a new run_id; sharing one run_id
    # between workers would make ownership impossible to prove.
    return job.status == JobStatus.running


async def _flush_errors(
    error_store: ErrorStore,
    errors: list[ErrorRecord],
    *,
    activity: RunActivityGuard | None = None,
) -> None:
    if not errors:
        return
    if activity is not None:
        await activity.checkpoint("before_error_flush")
    await error_store.log_errors(errors)
    if activity is not None:
        await activity.checkpoint("after_error_flush")
