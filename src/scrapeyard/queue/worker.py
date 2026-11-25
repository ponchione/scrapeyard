"""Stateless scrape task: orchestrates fetch -> validate -> store."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from datetime import timedelta

from scrapeyard.common.budgets import BudgetExceeded, RunBudget
from scrapeyard.common.ids import generate_run_id
from scrapeyard.common.json_encoding import compact_json_size
from scrapeyard.common.qualification import qualification_checkpoint
from scrapeyard.common.run_threads import run_thread_work
from scrapeyard.common.settings import get_settings
from scrapeyard.common.time import utc_now
from scrapeyard.config.loader import load_config
from scrapeyard.config.schema import FailStrategy, PageCacheMode
from scrapeyard.engine.domain_guard import (
    DomainGuard,
    RunDomainPolicy,
    effective_daily_page_limit,
)
from scrapeyard.engine.page_cache import PageCache
from scrapeyard.engine.rate_limiter import DomainRateLimiter
from scrapeyard.engine.resilience import CircuitBreaker
from scrapeyard.engine.scraper import TargetResult, scrape_target
from scrapeyard.engine.url_guard import (
    activate_deployment_secret_redaction,
    reset_deployment_secret_redaction,
)
from scrapeyard.models.job import Job, JobStatus
from scrapeyard.queue.browser_limiter import BrowserExecutionLimiter
from scrapeyard.queue.cancellation import RunActivityGuard
from scrapeyard.queue.heartbeat import RunHeartbeat, RunHeartbeatLeaseLost
from scrapeyard.queue.run_lifecycle import build_run_paths, dispatch_webhook, save_run_result
from scrapeyard.queue.worker_context import (
    JobExecutionContext,
    OwnerFinalization,
    PersistedJobResult,
    TargetProcessingContext,
)
from scrapeyard.queue.worker_output import (
    annotate_run_output,
    collect_result_payload,
    determine_final_status,
    materialize_output_artifact,
)
from scrapeyard.queue.worker_recovery import (
    discard_unowned_result,
    exception_detail,
    handle_budget_stop,
    handle_execution_crash,
    handle_worker_cancellation,
)
from scrapeyard.queue.worker_targets import process_all_targets
from scrapeyard.runtime.metrics import OUTPUT_BYTES, OUTPUT_RECORDS, observe_run
from scrapeyard.storage.protocols import ErrorStore, JobStore, ResultStore
from scrapeyard.storage.types import RunOwnershipError
from scrapeyard.webhook.dispatcher import WebhookNotifier
from scrapeyard.webhook.payload import build_terminal_webhook_delivery

__all__ = [
    "JobExecutionContext",
    "OwnerFinalization",
    "PersistedJobResult",
    "TargetProcessingContext",
    "scrape_task",
]

logger = logging.getLogger(__name__)


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
    domain_guard: DomainGuard | None = None,
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
        context = await _load_job_execution_context(
            job_id,
            config_yaml,
            run_id,
            job_store,
            domain_guard=domain_guard,
        )
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
        all_results = await process_all_targets(
            context=context,
            job_id=job_id,
            run_id=context.run_id,
            circuit_breaker=circuit_breaker,
            rate_limiter=rate_limiter,
            browser_limiter=browser_limiter,
            error_store=error_store,
            scrape=scrape_target,
            monotonic=time.monotonic,
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
            error_store=error_store,
            webhook_dispatcher=webhook_dispatcher,
        )
        metric_status = persisted.final_status.value
    except BudgetExceeded as exc:
        metric_status = "failed"
        try:
            await handle_budget_stop(
                job_id=job_id,
                run_id=active_run_id,
                context=context,
                heartbeat=heartbeat,
                job_store=job_store,
                result_store=result_store,
                error_store=error_store,
                webhook_dispatcher=webhook_dispatcher,
                error=exc,
                now_fn=utc_now,
            )
        except asyncio.CancelledError:
            metric_status = "cancelled"
            if await handle_worker_cancellation(
                job_id=job_id,
                run_id=active_run_id,
                context=context,
                heartbeat=heartbeat,
                job_store=job_store,
                result_store=result_store,
                error_store=error_store,
                webhook_dispatcher=webhook_dispatcher,
                now_fn=utc_now,
            ):
                raise
    except asyncio.CancelledError:
        metric_status = "cancelled"
        if await handle_worker_cancellation(
            job_id=job_id,
            run_id=active_run_id,
            context=context,
            heartbeat=heartbeat,
            job_store=job_store,
            result_store=result_store,
            error_store=error_store,
            webhook_dispatcher=webhook_dispatcher,
            now_fn=utc_now,
        ):
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
        await discard_unowned_result(
            result_store, job_id, active_run_id, job_store=job_store, context=context
        )
    except Exception as exc:
        metric_status = "failed"
        try:
            if heartbeat is not None:
                await heartbeat.stop()
            logger.error(
                "scrape_task crashed for job_id=%s run_id=%s: %s",
                job_id,
                active_run_id,
                exception_detail(exc),
            )
            await discard_unowned_result(
                result_store, job_id, active_run_id, job_store=job_store, context=context
            )
            await handle_execution_crash(
                context=context,
                job_id=job_id,
                run_id=active_run_id,
                job_store=job_store,
                error_store=error_store,
                webhook_dispatcher=webhook_dispatcher,
                now_fn=utc_now,
            )
        except asyncio.CancelledError:
            metric_status = "cancelled"
            if await handle_worker_cancellation(
                job_id=job_id,
                run_id=active_run_id,
                context=context,
                heartbeat=heartbeat,
                job_store=job_store,
                result_store=result_store,
                error_store=error_store,
                webhook_dispatcher=webhook_dispatcher,
                now_fn=utc_now,
            ):
                raise
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


async def _load_job_execution_context(
    job_id: str,
    config_yaml: str,
    run_id: str | None,
    job_store: JobStore,
    *,
    domain_guard: DomainGuard | None = None,
) -> JobExecutionContext | None:
    job = await job_store.get_job(job_id)
    effective_run_id = run_id or job.current_run_id or generate_run_id()
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
    config = await asyncio.to_thread(load_config, config_yaml)
    settings = get_settings()
    adaptive_dir, run_artifacts_dir = build_run_paths(
        settings,
        config.project,
        job.name,
        effective_run_id,
    )
    started_at = utc_now()
    budget = RunBudget.from_settings(settings, execution=config.execution)
    if config.execution.deadline_at is not None:
        remaining = max(0, (config.execution.deadline_at - started_at).total_seconds())
        budget.max_duration_seconds = min(budget.max_duration_seconds, remaining)
        budget.deadline_monotonic = budget.started_monotonic + budget.max_duration_seconds
    domain_policy = None
    if domain_guard is not None:
        domain_policy = RunDomainPolicy(
            guard=domain_guard,
            daily_page_limit=effective_daily_page_limit(
                settings.domain_daily_page_limit,
                config.execution.domain_daily_page_limit,
            ),
            cooldown_seconds=settings.domain_denial_cooldown_seconds,
        )
    page_cache = None
    page_cache_mode = config.execution.page_cache
    if page_cache_mode is not PageCacheMode.off and settings.page_cache_dir:
        page_cache = PageCache.for_project(page_cache_mode, settings.page_cache_dir, config.project)
    return JobExecutionContext(
        config=config,
        config_hash=hashlib.sha256(config_yaml.encode()).hexdigest(),
        job=job,
        settings=settings,
        started_at=started_at,
        adaptive_dir=adaptive_dir,
        run_artifacts_dir=run_artifacts_dir,
        budget=budget,
        run_id=effective_run_id,
        activity=RunActivityGuard(
            job_store=job_store,
            job_id=job_id,
            run_id=effective_run_id,
        ),
        domain_policy=domain_policy,
        page_cache=page_cache,
        page_cache_unavailable=page_cache_mode is not PageCacheMode.off and page_cache is None,
    )


async def _mark_run_started(
    context: JobExecutionContext,
    job_id: str,
    run_id: str,
    trigger: str,
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
    claimed = await job_store.claim_run(
        run_id,
        job_id,
        trigger,
        context.config_hash,
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
    flat_data, all_errors = collect_result_payload(all_results)
    final_status = determine_final_status(context.config, all_results, flat_data)
    publish_results = not (
        final_status == JobStatus.failed
        and context.config.execution.fail_strategy == FailStrategy.all_or_nothing
    )
    if not publish_results:
        flat_data.clear()

    output_data = await run_thread_work(
        materialize_output_artifact,
        context.config,
        all_results,
        job_id,
        final_status,
        all_errors,
        publish_results=publish_results,
        budget=context.budget,
        now_fn=utc_now,
        size_fn=compact_json_size,
        run_budget=context.budget,
    )
    context.budget.check_deadline()
    output_data["run_budget"] = context.budget.snapshot()
    annotate_run_output(output_data, context)
    save_meta = await save_run_result(
        job_id=job_id,
        run_id=run_id,
        config_hash=context.config_hash,
        result_store=result_store,
        output_data=output_data,
        final_status=final_status,
        record_count=len(flat_data),
        budget=context.budget,
    )
    if isinstance(save_meta.record_count, int) and save_meta.record_count > 0:
        OUTPUT_RECORDS.inc(save_meta.record_count)
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
    error_store: ErrorStore,
    webhook_dispatcher: WebhookNotifier | None,
) -> None:
    await context.activity.checkpoint("before_terminal_finalization")
    completed_at = utc_now()
    error_count = await error_store.count_errors_for_run(run_id)
    webhook_delivery = build_terminal_webhook_delivery(
        config=context.config,
        config_hash=context.config_hash,
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
        context.finalization.completed_at = completed_at
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
