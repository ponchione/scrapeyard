"""Stateless scrape task: orchestrates fetch → validate → store."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from scrapeyard.common.settings import ServiceSettings, get_settings
from scrapeyard.common.time import utc_now
from scrapeyard.config.loader import load_config
from scrapeyard.config.schema import FailStrategy, GroupBy, ScrapeConfig, TargetConfig
from scrapeyard.engine.fetch_classifier import classify_fetch_exception
from scrapeyard.engine.rate_limiter import DomainRateLimiter
from scrapeyard.engine.resilience import CircuitBreaker, ResultValidator
from scrapeyard.engine.scraper import TargetResult, TargetStatus, scrape_target
from scrapeyard.engine.url_guard import (
    redact_sensitive_mapping,
    redact_userinfo_in_text,
    redact_userinfo_in_url,
    url_host_label,
)
from scrapeyard.models.job import ErrorRecord, Job, JobStatus
from scrapeyard.queue.error_records import TargetErrorRecorder
from scrapeyard.queue.job_state import run_lease_is_active
from scrapeyard.queue.run_lifecycle import (
    build_run_paths,
    create_run_record,
    dispatch_webhook,
    finalize_run,
    handle_crash,
    mark_job_running,
    save_run_result,
    update_job_completion,
)
from scrapeyard.queue.target_execution import (
    TargetRuntimeContext,
    guard_target_execution,
    log_target_fetch,
    record_failed_target,
    resolve_target_runtime_context,
)
from scrapeyard.queue.validation_policy import apply_validation
from scrapeyard.storage.protocols import ErrorStore, JobStore, ResultStore
from scrapeyard.storage.types import SaveResultMeta
from scrapeyard.webhook.dispatcher import WebhookDispatcher

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobExecutionContext:
    config: ScrapeConfig
    job: Job
    settings: ServiceSettings
    started_at: datetime
    adaptive_dir: str
    run_artifacts_dir: str | None


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
    webhook_dispatcher: WebhookDispatcher | None = None,
) -> None:
    """Execute a complete scrape job."""
    try:
        context = await _load_job_execution_context(job_id, config_yaml, run_id, job_store)
        if context is None:
            return

        await _mark_run_started(context, job_id, run_id, trigger, config_yaml, job_store)
        all_results = await _process_all_targets(
            context=context,
            job_id=job_id,
            run_id=run_id,
            circuit_breaker=circuit_breaker,
            rate_limiter=rate_limiter,
            error_store=error_store,
        )
        persisted = await _persist_job_results(
            context=context,
            job_id=job_id,
            run_id=run_id,
            all_results=all_results,
            job_store=job_store,
            result_store=result_store,
            error_store=error_store,
        )
        if persisted is None:
            return

        await _finalize_job_execution(
            context=context,
            job_id=job_id,
            run_id=run_id,
            persisted=persisted,
            job_store=job_store,
            webhook_dispatcher=webhook_dispatcher,
        )
    except Exception as exc:
        logger.error("scrape_task crashed for job_id=%s: %s", job_id, _exception_detail(exc))
        await handle_crash(job_id, run_id, job_store)


async def _load_job_execution_context(
    job_id: str,
    config_yaml: str,
    run_id: str | None,
    job_store: JobStore,
) -> JobExecutionContext | None:
    started_at = utc_now()
    config = await asyncio.to_thread(load_config, config_yaml)
    job = await job_store.get_job(job_id)
    settings = get_settings()
    adaptive_dir, run_artifacts_dir = build_run_paths(
        settings,
        config.project,
        job.name,
        run_id,
    )
    if _should_skip_delivery(job, run_id, settings.workers_running_lease_seconds, started_at):
        logger.info("Skipping duplicate or superseded delivery for job_id=%s run_id=%s", job_id, run_id)
        return None
    return JobExecutionContext(
        config=config,
        job=job,
        settings=settings,
        started_at=started_at,
        adaptive_dir=adaptive_dir,
        run_artifacts_dir=run_artifacts_dir,
    )


async def _mark_run_started(
    context: JobExecutionContext,
    job_id: str,
    run_id: str | None,
    trigger: str,
    config_yaml: str,
    job_store: JobStore,
) -> None:
    await mark_job_running(job_store, context.job, context.started_at)
    await create_run_record(
        job_store,
        run_id=run_id,
        job_id=job_id,
        trigger=trigger,
        config_yaml=config_yaml,
        started_at=context.started_at,
    )


async def _persist_job_results(
    *,
    context: JobExecutionContext,
    job_id: str,
    run_id: str | None,
    all_results: list[TargetResult],
    job_store: JobStore,
    result_store: ResultStore,
    error_store: ErrorStore,
) -> PersistedJobResult | None:
    flat_data, all_errors = _collect_result_payload(all_results)
    final_status = _determine_final_status(context.config, all_results, flat_data)
    if final_status == JobStatus.failed and context.config.execution.fail_strategy == FailStrategy.all_or_nothing:
        flat_data.clear()

    latest_job = await job_store.get_job(job_id)
    if _run_superseded(latest_job, run_id):
        logger.info("Skipping result save for superseded job_id=%s run_id=%s", job_id, run_id)
        return None

    output_data = _format_output(context.config, all_results, flat_data, job_id, final_status, all_errors)
    save_meta = await save_run_result(
        job_id=job_id,
        run_id=run_id,
        result_store=result_store,
        output_data=output_data,
        final_status=final_status,
        record_count=len(flat_data),
    )
    await finalize_run(run_id, final_status, len(flat_data), job_store, error_store)
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
    run_id: str | None,
    persisted: PersistedJobResult,
    job_store: JobStore,
    webhook_dispatcher: WebhookDispatcher | None,
) -> None:
    completed_at = utc_now()
    latest_job = await job_store.get_job(job_id)
    if _run_superseded(latest_job, run_id):
        logger.info("Skipping finalization for superseded job_id=%s run_id=%s", job_id, run_id)
        return

    await dispatch_webhook(
        webhook_dispatcher=webhook_dispatcher,
        config=context.config,
        job_id=job_id,
        final_status=persisted.final_status,
        save_meta=persisted.save_meta,
        all_errors=persisted.all_errors,
        started_at=context.started_at,
        completed_at=completed_at,
    )
    await update_job_completion(job_store, latest_job, persisted.final_status, completed_at, run_id)


async def _process_all_targets(
    *,
    context: JobExecutionContext,
    job_id: str,
    run_id: str | None,
    circuit_breaker: CircuitBreaker,
    rate_limiter: DomainRateLimiter,
    error_store: ErrorStore,
) -> list[TargetResult]:
    """Dispatch all targets with concurrency, delay, and rate limiting."""
    config = context.config
    targets = list(config.resolved_targets())
    sem = asyncio.Semaphore(config.execution.concurrency)
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
    )

    async def _process_one(target_cfg: TargetConfig) -> TargetResult:
        pending_errors: list[ErrorRecord] = []
        try:
            async with sem:
                return await _fetch_and_validate_target(
                    target_cfg=target_cfg,
                    context=target_context,
                    pending_errors=pending_errors,
                )
        finally:
            await _flush_errors(error_store, pending_errors)

    tasks: list[asyncio.Task[TargetResult]] = []
    for i, target_cfg in enumerate(targets):
        if i > 0 and config.execution.delay_between > 0:
            await asyncio.sleep(config.execution.delay_between)
        tasks.append(asyncio.create_task(_process_one(target_cfg)))

    outcomes = await asyncio.gather(*tasks, return_exceptions=True)

    all_results: list[TargetResult] = []
    task_errors: list[Exception] = []
    for outcome in outcomes:
        if isinstance(outcome, TargetResult):
            all_results.append(outcome)
        elif isinstance(outcome, asyncio.CancelledError):
            raise outcome
        elif isinstance(outcome, Exception):
            task_errors.append(outcome)
        elif isinstance(outcome, BaseException):
            raise outcome
        else:
            task_errors.append(TypeError(f"Unexpected target task result: {type(outcome).__name__}"))

    if task_errors:
        raise task_errors[0]
    return all_results


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
    )
    if circuit_open is not None:
        return TargetResult(url=target_cfg.url, status=TargetStatus.failed, errors=[str(circuit_open)])

    log_target_fetch(target_cfg, runtime)
    try:
        result = await scrape_target(
            target_cfg,
            runtime.adaptive,
            context.config.retry,
            adaptive_dir=context.adaptive_dir,
            proxy_url=runtime.proxy_url,
            artifacts_dir=runtime.artifacts_dir,
        )
        if not result.is_success:
            record_failed_target(
                runtime=runtime,
                result=result,
                target_cfg=target_cfg,
                recorder=recorder,
            )
            return result

        recorder.record_success(runtime.domain)
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
        )
    except Exception as exc:
        return _target_exception_result(
            runtime=runtime,
            target_cfg=target_cfg,
            recorder=recorder,
            exc=exc,
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


def _target_result_details(result: TargetResult) -> dict[str, Any]:
    return {
        "status": result.status_value,
        "count": len(result.data),
        "debug": redact_sensitive_mapping(result.debug) if result.debug is not None else None,
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
    flat_data: list[dict[str, Any]],
    job_id: str,
    final_status: JobStatus,
    all_errors: list[str],
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
                **_target_result_details(result),
                "pages_scraped": result.pages_scraped,
                "errors": [redact_userinfo_in_text(error) for error in result.errors],
            }
            for result in all_results
        ],
    }

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


def _run_superseded(job: Any, run_id: str | None) -> bool:
    return run_id is not None and job.current_run_id != run_id


def _should_skip_delivery(
    job: Any,
    run_id: str | None,
    running_lease_seconds: int,
    now: datetime,
) -> bool:
    if _run_superseded(job, run_id):
        return True
    if job.status in {JobStatus.complete, JobStatus.partial, JobStatus.failed}:
        return run_id is None or job.current_run_id == run_id
    if job.status != JobStatus.running:
        return False
    return run_lease_is_active(
        cast(datetime | None, job.updated_at),
        lease_seconds=running_lease_seconds,
        now=now,
    )


async def _flush_errors(error_store: ErrorStore, errors: list[ErrorRecord]) -> None:
    if not errors:
        return
    await error_store.log_errors(errors)
