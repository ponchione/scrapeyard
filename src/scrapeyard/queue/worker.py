"""Stateless scrape task: orchestrates fetch → validate → store."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

from scrapeyard.common.settings import get_settings
from scrapeyard.config.loader import load_config
from scrapeyard.config.schema import (
    FailStrategy,
    GroupBy,
    OnEmptyAction,
    ScrapeConfig,
    TargetConfig,
)
from scrapeyard.engine.proxy import redact_proxy_url, resolve_proxy
from scrapeyard.engine.rate_limiter import DomainRateLimiter
from scrapeyard.engine.resilience import CircuitBreaker, CircuitOpenError, ResultValidator
from scrapeyard.engine.scraper import TargetResult, scrape_target
from scrapeyard.models.job import ActionTaken, ErrorRecord, ErrorType, JobStatus
from scrapeyard.storage.protocols import ErrorStore, JobStore, ResultStore
from scrapeyard.webhook.dispatcher import WebhookDispatcher
from scrapeyard.webhook.payload import build_webhook_payload, should_fire

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TargetRuntimeContext:
    domain: str
    adaptive: bool
    proxy_url: str | None
    artifacts_dir: str | None


# ---------------------------------------------------------------------------
# scrape_task — top-level orchestrator
# ---------------------------------------------------------------------------


def _build_run_paths(settings: Any, project: str, job_name: str, run_id: str | None) -> tuple[str, str | None]:
    adaptive_dir = str(Path(settings.adaptive_dir) / project)
    run_artifacts_dir = None if run_id is None else str(
        Path(settings.storage_results_dir) / project / job_name / run_id / "artifacts"
    )
    return adaptive_dir, run_artifacts_dir


async def _mark_job_running(job_store: JobStore, job: Any, started_at: datetime) -> Any:
    running_job = job.model_copy(update={
        "status": JobStatus.running,
        "updated_at": started_at,
    })
    await job_store.update_job_status(running_job)
    return running_job


async def _create_run_record(
    job_store: JobStore,
    *,
    run_id: str | None,
    job_id: str,
    trigger: str,
    config_yaml: str,
    started_at: datetime,
) -> None:
    if run_id is None:
        return
    config_hash = hashlib.sha256(config_yaml.encode()).hexdigest()
    await job_store.create_run(run_id, job_id, trigger, config_hash, started_at)


def _collect_result_payload(all_results: list[TargetResult]) -> tuple[list[dict[str, Any]], list[str]]:
    flat_data: list[dict[str, Any]] = []
    all_errors: list[str] = []
    for target_result in all_results:
        flat_data.extend(target_result.data)
        all_errors.extend(target_result.errors)
    return flat_data, all_errors


async def _save_run_result(
    *,
    job_id: str,
    run_id: str | None,
    result_store: ResultStore,
    output_data: dict[str, Any],
    final_status: JobStatus,
    record_count: int,
) -> Any:
    return await result_store.save_result(
        job_id,
        output_data,
        run_id=run_id,
        status=final_status.value,
        record_count=record_count,
    )


async def _update_job_completion(
    job_store: JobStore,
    job: Any,
    final_status: JobStatus,
    completed_at: datetime,
    run_id: str | None,
) -> None:
    completed_job = job.model_copy(update={
        "status": final_status,
        "updated_at": completed_at,
        "current_run_id": run_id,
    })
    await job_store.update_job_status(completed_job)


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
    """Execute a complete scrape job.

    This is the top-level worker function that:

    1. Parses the YAML config.
    2. Updates job status to *running*.
    3. Iterates resolved targets, respecting concurrency / delay / rate limits.
    4. Applies circuit breaker per domain.
    5. Validates results.
    6. Saves output via *result_store*.
    7. Logs errors via *error_store*.
    8. Updates final job status.
    """
    try:
        started_at = datetime.now(timezone.utc)
        config = load_config(config_yaml)
        job = await job_store.get_job(job_id)

        settings = get_settings()
        adaptive_dir, run_artifacts_dir = _build_run_paths(
            settings, config.project, job.name, run_id
        )
        if _should_skip_delivery(job, run_id, settings.workers_running_lease_seconds, started_at):
            logger.info("Skipping duplicate or superseded delivery for job_id=%s run_id=%s", job_id, run_id)
            return

        await _mark_job_running(job_store, job, started_at)
        await _create_run_record(
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
        if (
            final_status == JobStatus.failed
            and config.execution.fail_strategy == FailStrategy.all_or_nothing
        ):
            flat_data.clear()

        latest_job = await job_store.get_job(job_id)
        if _run_superseded(latest_job, run_id):
            logger.info("Skipping result save for superseded job_id=%s run_id=%s", job_id, run_id)
            return

        output_data = _format_output(config, all_results, flat_data, job_id, final_status, all_errors)
        save_meta = await _save_run_result(
            job_id=job_id,
            run_id=run_id,
            result_store=result_store,
            output_data=output_data,
            final_status=final_status,
            record_count=len(flat_data),
        )
        await _finalize_run(run_id, final_status, len(flat_data), job_store, error_store)

        completed_at = datetime.now(timezone.utc)
        latest_job = await job_store.get_job(job_id)
        if _run_superseded(latest_job, run_id):
            logger.info("Skipping finalization for superseded job_id=%s run_id=%s", job_id, run_id)
            return

        await _dispatch_webhook(
            webhook_dispatcher=webhook_dispatcher,
            config=config,
            job_id=job_id,
            final_status=final_status,
            save_meta=save_meta,
            all_errors=all_errors,
            started_at=started_at,
            completed_at=completed_at,
        )
        await _update_job_completion(job_store, latest_job, final_status, completed_at, run_id)
    except Exception:
        logger.exception("scrape_task crashed for job_id=%s", job_id)
        await _handle_crash(job_id, run_id, job_store)


# ---------------------------------------------------------------------------
# Target processing
# ---------------------------------------------------------------------------


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
    concurrency = config.execution.concurrency
    delay_between = config.execution.delay_between
    sem = asyncio.Semaphore(concurrency)
    validator = ResultValidator(config.validation)

    async def _process_one(target_cfg: TargetConfig) -> TargetResult:
        pending_errors: list[ErrorRecord] = []
        try:
            async with sem:
                result = await _fetch_and_validate_target(
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
                return result
        finally:
            await _flush_errors(error_store, pending_errors)

    tasks: list[asyncio.Task] = []
    for i, t in enumerate(targets):
        if i > 0 and delay_between > 0:
            await asyncio.sleep(delay_between)
        tasks.append(asyncio.create_task(_process_one(t)))

    all_results: list[TargetResult] = []
    for task in tasks:
        all_results.append(await task)
    return all_results


def _resolve_target_runtime_context(
    *,
    target_cfg: TargetConfig,
    config: ScrapeConfig,
    settings: Any,
    run_artifacts_dir: str | None,
) -> TargetRuntimeContext:
    domain = urlparse(target_cfg.url).netloc
    adaptive = config.adaptive if config.adaptive is not None else config.schedule is not None
    proxy_url = resolve_proxy(target_cfg, config.proxy, settings.proxy_url)
    artifacts_dir = None if run_artifacts_dir is None else str(Path(run_artifacts_dir) / domain)
    return TargetRuntimeContext(
        domain=domain,
        adaptive=adaptive,
        proxy_url=proxy_url,
        artifacts_dir=artifacts_dir,
    )


async def _guard_target_execution(
    *,
    runtime: TargetRuntimeContext,
    config: ScrapeConfig,
    target_cfg: TargetConfig,
    job_id: str,
    run_id: str | None,
    circuit_breaker: CircuitBreaker,
    rate_limiter: DomainRateLimiter,
    pending_errors: list[ErrorRecord],
) -> CircuitOpenError | None:
    try:
        circuit_breaker.check(runtime.domain)
    except CircuitOpenError as exc:
        logger.info("Circuit breaker open for %s", runtime.domain)
        pending_errors.append(_build_error_record(
            job_id, run_id or "", config.project, target_cfg.url,
            0, ErrorType.network_error, None, "circuit_breaker",
            ActionTaken.circuit_break,
        ))
        return exc

    await rate_limiter.acquire(runtime.domain, config.execution.domain_rate_limit)
    return None


def _log_target_fetch(target_cfg: TargetConfig, runtime: TargetRuntimeContext) -> None:
    if runtime.proxy_url:
        logger.info(
            "Scraping %s with fetcher=%s adaptive=%s proxy=%s",
            target_cfg.url, target_cfg.fetcher.value, runtime.adaptive,
            redact_proxy_url(runtime.proxy_url),
        )
    else:
        logger.info(
            "Scraping %s with fetcher=%s adaptive=%s",
            target_cfg.url, target_cfg.fetcher.value, runtime.adaptive,
        )


def _record_failed_target(
    *,
    runtime: TargetRuntimeContext,
    result: TargetResult,
    pending_errors: list[ErrorRecord],
    config: ScrapeConfig,
    target_cfg: TargetConfig,
    job_id: str,
    run_id: str | None,
    circuit_breaker: CircuitBreaker,
) -> None:
    logger.warning(
        "Recording failure for domain %s type=%s status=%s detail=%s",
        runtime.domain,
        result.error_type.value if result.error_type else ErrorType.http_error.value,
        result.http_status,
        result.error_detail or "; ".join(result.errors) or "unknown error",
    )
    circuit_breaker.record_failure(runtime.domain)
    for err_msg in result.errors or [result.error_detail or "unknown scrape failure"]:
        pending_errors.append(_build_error_record(
            job_id, run_id or "", config.project, target_cfg.url,
            1, result.error_type or ErrorType.http_error,
            result.http_status, target_cfg.fetcher.value,
            ActionTaken.fail, error_message=err_msg,
        ))


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
    runtime = _resolve_target_runtime_context(
        target_cfg=target_cfg,
        config=config,
        settings=settings,
        run_artifacts_dir=run_artifacts_dir,
    )
    circuit_open = await _guard_target_execution(
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

    _log_target_fetch(target_cfg, runtime)
    result = await scrape_target(
        target_cfg, runtime.adaptive, config.retry,
        adaptive_dir=adaptive_dir, proxy_url=runtime.proxy_url,
        artifacts_dir=runtime.artifacts_dir,
    )

    if result.status != "success":
        _record_failed_target(
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
    return await _apply_validation(
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
        proxy_url=runtime.proxy_url,
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


async def _apply_validation(
    *,
    target_cfg: TargetConfig,
    domain: str,
    adaptive: bool,
    result: TargetResult,
    pending_errors: list[ErrorRecord],
    config: ScrapeConfig,
    adaptive_dir: str,
    run_artifacts_dir: str | None,
    job_id: str,
    run_id: str | None,
    circuit_breaker: CircuitBreaker,
    validator: ResultValidator,
    proxy_url: str | None = None,
    attempt: int = 1,
) -> TargetResult:
    """Validate a successful result; retry once on validation failure."""
    if result.status != "success":
        return result

    validation = validator.validate(result.data)
    if validation.passed:
        return result

    action = ActionTaken(validation.action.value)
    pending_errors.append(_build_error_record(
        job_id, run_id or "", config.project, target_cfg.url,
        attempt, _validation_error_type(result), None,
        target_cfg.fetcher.value, action,
        error_message=validation.message,
    ))

    if validation.action == OnEmptyAction.warn:
        logger.warning("Validation warning for %s: %s", target_cfg.url, validation.message)
        result.errors.append(validation.message)
        return result

    if validation.action == OnEmptyAction.skip:
        logger.info("Skipping invalid result for %s: %s", target_cfg.url, validation.message)
        return TargetResult(
            url=target_cfg.url, status="success", data=[],
            errors=[validation.message],
            pages_scraped=result.pages_scraped, debug=result.debug,
        )

    if validation.action == OnEmptyAction.fail:
        logger.info("Failing target %s due to validation: %s", target_cfg.url, validation.message)
        return TargetResult(
            url=target_cfg.url, status="failed", data=[],
            errors=[validation.message],
            pages_scraped=result.pages_scraped,
            error_type=_validation_error_type(result),
            error_detail=validation.message, debug=result.debug,
        )

    # Retry path.
    logger.info("Retrying target %s after validation failure: %s", target_cfg.url, validation.message)
    artifacts_dir = None if run_artifacts_dir is None else str(Path(run_artifacts_dir) / domain)
    retry_result = await scrape_target(
        target_cfg, adaptive, config.retry,
        adaptive_dir=adaptive_dir, proxy_url=proxy_url,
        artifacts_dir=artifacts_dir,
    )
    if retry_result.status != "success":
        logger.info("Recording failure for domain %s after validation retry", domain)
        circuit_breaker.record_failure(domain)
        for _ in retry_result.errors:
            pending_errors.append(_build_error_record(
                job_id, run_id or "", config.project, target_cfg.url,
                attempt + 1, retry_result.error_type or ErrorType.http_error,
                retry_result.http_status, target_cfg.fetcher.value,
                ActionTaken.fail,
                error_message=retry_result.error_detail or "; ".join(retry_result.errors),
            ))
        return retry_result

    circuit_breaker.record_success(domain)
    retry_validation = validator.validate(retry_result.data)
    if retry_validation.passed:
        return retry_result

    pending_errors.append(_build_error_record(
        job_id, run_id or "", config.project, target_cfg.url,
        attempt + 1, _validation_error_type(retry_result), None,
        target_cfg.fetcher.value, ActionTaken.fail,
        error_message=retry_validation.message,
    ))
    return TargetResult(
        url=target_cfg.url, status="failed", data=[],
        errors=[retry_validation.message],
        pages_scraped=retry_result.pages_scraped,
        error_type=_validation_error_type(retry_result),
        error_detail=retry_validation.message, debug=retry_result.debug,
    )


# ---------------------------------------------------------------------------
# Status determination
# ---------------------------------------------------------------------------


def _determine_final_status(
    config: ScrapeConfig,
    all_results: list[TargetResult],
    flat_data: list[dict[str, Any]],
) -> JobStatus:
    """Determine the final job status based on results and fail_strategy.

    Pure read-only function — does NOT mutate *flat_data*.  The caller is
    responsible for discarding data when the returned status is ``failed``
    under ``all_or_nothing`` strategy.
    """
    failed_count = sum(1 for r in all_results if r.status == "failed")
    fail_strategy = config.execution.fail_strategy

    if fail_strategy == FailStrategy.all_or_nothing:
        if failed_count > 0:
            return JobStatus.failed
        return JobStatus.complete
    elif fail_strategy == FailStrategy.continue_:
        return JobStatus.complete if flat_data else JobStatus.failed
    else:
        # FailStrategy.partial (default).
        if failed_count == len(all_results) or not flat_data:
            return JobStatus.failed
        elif failed_count > 0:
            return JobStatus.partial
        return JobStatus.complete


# ---------------------------------------------------------------------------
# Result formatting
# ---------------------------------------------------------------------------


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
                "url": tr.url,
                "status": tr.status,
                "count": len(tr.data),
                "pages_scraped": tr.pages_scraped,
                "error_type": tr.error_type.value if tr.error_type else None,
                "error_detail": tr.error_detail,
                "errors": tr.errors,
                "debug": tr.debug,
            }
            for tr in all_results
        ],
    }

    group_by = config.output.group_by
    if group_by == GroupBy.merge:
        merged: list[Any] = []
        for tr in all_results:
            for item in tr.data:
                if isinstance(item, dict):
                    item["_source"] = urlparse(tr.url).netloc
                merged.append(item)
        return {**job_meta, "results": merged}

    grouped: dict[str, Any] = {}
    for tr in all_results:
        domain = urlparse(tr.url).netloc
        grouped[domain] = {
            "status": tr.status,
            "count": len(tr.data),
            "data": tr.data,
            "debug": tr.debug,
            "error_type": tr.error_type.value if tr.error_type else None,
            "error_detail": tr.error_detail,
        }
    return {**job_meta, "results": grouped}


# ---------------------------------------------------------------------------
# Run finalization
# ---------------------------------------------------------------------------


async def _finalize_run(
    run_id: str | None,
    final_status: JobStatus,
    record_count: int,
    job_store: JobStore,
    error_store: ErrorStore,
) -> None:
    """Finalize a run row with status, counts, and completed_at.

    Cross-DB safety: error_count comes from errors.db, finalize_run writes
    to jobs.db.  If the write fails the run would be stuck in ``running``
    forever, so we catch the exception and fall back to ``fail_run``.
    """
    if run_id is None:
        return
    error_count = await error_store.count_errors_for_run(run_id)
    try:
        await job_store.finalize_run(run_id, final_status.value, record_count, error_count)
    except Exception:
        logger.critical(
            "Failed to finalize run %s (status=%s) — falling back to fail_run",
            run_id,
            final_status.value,
            exc_info=True,
        )
        try:
            await job_store.fail_run(run_id)
        except Exception:
            logger.critical(
                "fail_run fallback also failed for run %s — run stuck in 'running'",
                run_id,
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Webhook dispatch
# ---------------------------------------------------------------------------


async def _dispatch_webhook(
    *,
    webhook_dispatcher: WebhookDispatcher | None,
    config: ScrapeConfig,
    job_id: str,
    final_status: JobStatus,
    save_meta: Any,
    all_errors: list[str],
    started_at: datetime,
    completed_at: datetime,
) -> None:
    """Submit webhook payload if configured and status matches trigger conditions."""
    if webhook_dispatcher is None or config.webhook is None:
        return
    if not should_fire(config.webhook, final_status):
        return
    payload = build_webhook_payload(
        job_id=job_id,
        project=config.project,
        name=config.name,
        status=final_status,
        run_id=save_meta.run_id if save_meta else None,
        result_path=save_meta.file_path if save_meta else None,
        result_count=save_meta.record_count if save_meta else 0,
        error_count=len(all_errors),
        started_at=started_at.isoformat(),
        completed_at=completed_at.isoformat(),
    )
    await webhook_dispatcher.submit(config.webhook, payload)


# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------


async def _handle_crash(
    job_id: str,
    run_id: str | None,
    job_store: JobStore,
) -> None:
    """Best-effort crash recovery: mark job and run as failed."""
    try:
        job = await job_store.get_job(job_id)
        job = job.model_copy(update={
            "status": JobStatus.failed,
            "updated_at": datetime.now(timezone.utc),
        })
        await job_store.update_job_status(job)
    except Exception:
        logger.exception("Failed to mark job %s as failed", job_id)

    if run_id is not None:
        try:
            await job_store.fail_run(run_id)
        except Exception:
            logger.exception("Failed to finalize run %s", run_id)


# ---------------------------------------------------------------------------
# Helpers (pure / near-pure)
# ---------------------------------------------------------------------------


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


def _validation_error_type(result: TargetResult) -> ErrorType:
    if result.error_type is not None:
        return result.error_type
    if result.debug and isinstance(result.debug, dict):
        classification = result.debug.get("classification")
        if classification is not None:
            try:
                return ErrorType(classification)
            except ValueError:
                pass
    return ErrorType.content_empty


def _build_error_record(
    job_id: str,
    run_id: str,
    project: str,
    url: str,
    attempt: int,
    error_type: ErrorType,
    http_status: int | None,
    fetcher_used: str,
    action: ActionTaken,
    error_message: str | None = None,
) -> ErrorRecord:
    """Build a structured error record for deferred persistence."""
    return ErrorRecord(
        job_id=job_id,
        run_id=run_id,
        project=project,
        target_url=url,
        attempt=attempt,
        error_type=error_type,
        http_status=http_status,
        fetcher_used=fetcher_used,
        error_message=error_message,
        action_taken=action,
    )


async def _flush_errors(
    error_store: ErrorStore, errors: list[ErrorRecord],
) -> None:
    if not errors:
        return
    await error_store.log_errors(errors)
