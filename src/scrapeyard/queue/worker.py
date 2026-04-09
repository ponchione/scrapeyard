"""Stateless scrape task: orchestrates fetch → validate → store."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, cast
from urllib.parse import urlparse

from scrapeyard.common.settings import get_settings
from scrapeyard.config.loader import load_config
from scrapeyard.config.schema import FailStrategy, GroupBy, ScrapeConfig, TargetConfig
from scrapeyard.engine.rate_limiter import DomainRateLimiter
from scrapeyard.engine.resilience import CircuitBreaker, ResultValidator
from scrapeyard.engine.scraper import TargetResult, scrape_target
from scrapeyard.models.job import ErrorRecord, JobStatus
from scrapeyard.queue.error_records import build_error_record, validation_error_type
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
    guard_target_execution,
    log_target_fetch,
    record_failed_target,
    resolve_target_runtime_context,
)
from scrapeyard.queue.validation_policy import apply_validation
from scrapeyard.storage.protocols import ErrorStore, JobStore, ResultStore
from scrapeyard.webhook.dispatcher import WebhookDispatcher

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
    webhook_dispatcher: WebhookDispatcher | None = None,
) -> None:
    """Execute a complete scrape job."""
    try:
        started_at = datetime.now(timezone.utc)
        config = load_config(config_yaml)
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
            return

        await mark_job_running(job_store, job, started_at)
        await create_run_record(
            job_store,
            run_id=run_id,
            job_id=job_id,
            trigger=trigger,
            config_yaml=config_yaml,
            started_at=started_at,
        )

        all_results = await _process_all_targets(
            config=config,
            job_id=job_id,
            run_id=run_id,
            adaptive_dir=adaptive_dir,
            run_artifacts_dir=run_artifacts_dir,
            settings=settings,
            circuit_breaker=circuit_breaker,
            rate_limiter=rate_limiter,
            error_store=error_store,
        )

        flat_data, all_errors = _collect_result_payload(all_results)
        final_status = _determine_final_status(config, all_results, flat_data)
        if final_status == JobStatus.failed and config.execution.fail_strategy == FailStrategy.all_or_nothing:
            flat_data.clear()

        latest_job = await job_store.get_job(job_id)
        if _run_superseded(latest_job, run_id):
            logger.info("Skipping result save for superseded job_id=%s run_id=%s", job_id, run_id)
            return

        output_data = _format_output(config, all_results, flat_data, job_id, final_status, all_errors)
        save_meta = await save_run_result(
            job_id=job_id,
            run_id=run_id,
            result_store=result_store,
            output_data=output_data,
            final_status=final_status,
            record_count=len(flat_data),
        )
        await finalize_run(run_id, final_status, len(flat_data), job_store, error_store)

        completed_at = datetime.now(timezone.utc)
        latest_job = await job_store.get_job(job_id)
        if _run_superseded(latest_job, run_id):
            logger.info("Skipping finalization for superseded job_id=%s run_id=%s", job_id, run_id)
            return

        await dispatch_webhook(
            webhook_dispatcher=webhook_dispatcher,
            config=config,
            job_id=job_id,
            final_status=final_status,
            save_meta=save_meta,
            all_errors=all_errors,
            started_at=started_at,
            completed_at=completed_at,
        )
        await update_job_completion(job_store, latest_job, final_status, completed_at, run_id)
    except Exception:
        logger.exception("scrape_task crashed for job_id=%s", job_id)
        await handle_crash(job_id, run_id, job_store)


async def _process_all_targets(
    *,
    config: ScrapeConfig,
    job_id: str,
    run_id: str | None,
    adaptive_dir: str,
    run_artifacts_dir: str | None,
    settings: Any,
    circuit_breaker: CircuitBreaker,
    rate_limiter: DomainRateLimiter,
    error_store: ErrorStore,
) -> list[TargetResult]:
    """Dispatch all targets with concurrency, delay, and rate limiting."""
    targets = config.resolved_targets()
    sem = asyncio.Semaphore(config.execution.concurrency)
    validator = ResultValidator(config.validation)

    async def _process_one(target_cfg: TargetConfig) -> TargetResult:
        pending_errors: list[ErrorRecord] = []
        try:
            async with sem:
                return await _fetch_and_validate_target(
                    target_cfg=target_cfg,
                    config=config,
                    job_id=job_id,
                    run_id=run_id,
                    adaptive_dir=adaptive_dir,
                    run_artifacts_dir=run_artifacts_dir,
                    settings=settings,
                    circuit_breaker=circuit_breaker,
                    rate_limiter=rate_limiter,
                    validator=validator,
                    pending_errors=pending_errors,
                )
        finally:
            await _flush_errors(error_store, pending_errors)

    tasks: list[asyncio.Task[TargetResult]] = []
    for i, target_cfg in enumerate(targets):
        if i > 0 and config.execution.delay_between > 0:
            await asyncio.sleep(config.execution.delay_between)
        tasks.append(asyncio.create_task(_process_one(target_cfg)))

    all_results: list[TargetResult] = []
    for task in tasks:
        all_results.append(await task)
    return all_results


async def _fetch_and_validate_target(
    *,
    target_cfg: TargetConfig,
    config: ScrapeConfig,
    job_id: str,
    run_id: str | None,
    adaptive_dir: str,
    run_artifacts_dir: str | None,
    settings: Any,
    circuit_breaker: CircuitBreaker,
    rate_limiter: DomainRateLimiter,
    validator: ResultValidator,
    pending_errors: list[ErrorRecord],
) -> TargetResult:
    """Fetch a single target and run validation. Returns the result."""
    runtime = resolve_target_runtime_context(
        target_cfg=target_cfg,
        config=config,
        settings=settings,
        run_artifacts_dir=run_artifacts_dir,
    )
    circuit_open = await guard_target_execution(
        runtime=runtime,
        config=config,
        target_cfg=target_cfg,
        job_id=job_id,
        run_id=run_id,
        circuit_breaker=circuit_breaker,
        rate_limiter=rate_limiter,
        pending_errors=pending_errors,
    )
    if circuit_open is not None:
        return TargetResult(url=target_cfg.url, status="failed", errors=[str(circuit_open)])

    log_target_fetch(target_cfg, runtime)
    result = await scrape_target(
        target_cfg,
        runtime.adaptive,
        config.retry,
        adaptive_dir=adaptive_dir,
        proxy_url=runtime.proxy_url,
        artifacts_dir=runtime.artifacts_dir,
    )

    if result.status != "success":
        record_failed_target(
            runtime=runtime,
            result=result,
            pending_errors=pending_errors,
            config=config,
            target_cfg=target_cfg,
            job_id=job_id,
            run_id=run_id,
            circuit_breaker=circuit_breaker,
        )
        return result

    circuit_breaker.record_success(runtime.domain)
    return await apply_validation(
        target_cfg=target_cfg,
        domain=runtime.domain,
        adaptive=runtime.adaptive,
        result=result,
        pending_errors=pending_errors,
        config=config,
        adaptive_dir=adaptive_dir,
        run_artifacts_dir=run_artifacts_dir,
        job_id=job_id,
        run_id=run_id,
        circuit_breaker=circuit_breaker,
        validator=validator,
        scrape=scrape_target,
        proxy_url=runtime.proxy_url,
    )


def _collect_result_payload(all_results: list[TargetResult]) -> tuple[list[dict[str, Any]], list[str]]:
    flat_data: list[dict[str, Any]] = []
    all_errors: list[str] = []
    for target_result in all_results:
        flat_data.extend(target_result.data)
        all_errors.extend(target_result.errors)
    return flat_data, all_errors


def _determine_final_status(
    config: ScrapeConfig,
    all_results: list[TargetResult],
    flat_data: list[dict[str, Any]],
) -> JobStatus:
    """Determine the final job status based on results and fail_strategy."""
    failed_count = sum(1 for result in all_results if result.status == "failed")
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
    job_meta: dict[str, Any] = {
        "project": config.project,
        "name": config.name,
        "job_id": job_id,
        "status": final_status.value,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "errors": all_errors,
        "targets": [
            {
                "url": result.url,
                "status": result.status,
                "count": len(result.data),
                "pages_scraped": result.pages_scraped,
                "error_type": result.error_type.value if result.error_type else None,
                "error_detail": result.error_detail,
                "errors": result.errors,
                "debug": result.debug,
            }
            for result in all_results
        ],
    }

    if config.output.group_by == GroupBy.merge:
        merged: list[Any] = []
        for result in all_results:
            for item in result.data:
                if isinstance(item, dict):
                    item["_source"] = urlparse(result.url).netloc
                merged.append(item)
        return {**job_meta, "results": merged}

    grouped: dict[str, Any] = {}
    for result in all_results:
        domain = urlparse(result.url).netloc
        grouped[domain] = {
            "status": result.status,
            "count": len(result.data),
            "data": result.data,
            "debug": result.debug,
            "error_type": result.error_type.value if result.error_type else None,
            "error_detail": result.error_detail,
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
    if job.updated_at is None:
        return False
    updated_at = cast(datetime, job.updated_at)
    lease_age = (now - updated_at).total_seconds()
    return lease_age < running_lease_seconds


async def _flush_errors(error_store: ErrorStore, errors: list[ErrorRecord]) -> None:
    if not errors:
        return
    await error_store.log_errors(errors)


# Backwards-compatible aliases for focused unit tests.
_build_run_paths = build_run_paths
_mark_job_running = mark_job_running
_create_run_record = create_run_record
_save_run_result = save_run_result
_update_job_completion = update_job_completion
_finalize_run = finalize_run
_dispatch_webhook = dispatch_webhook
_handle_crash = handle_crash
_resolve_target_runtime_context = resolve_target_runtime_context
_guard_target_execution = guard_target_execution
_log_target_fetch = log_target_fetch
_record_failed_target = record_failed_target
_apply_validation = apply_validation
_build_error_record = build_error_record
_validation_error_type = validation_error_type
