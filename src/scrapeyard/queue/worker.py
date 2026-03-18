"""Stateless scrape task: orchestrates fetch → validate → format → store."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from scrapeyard.common.settings import get_settings
from scrapeyard.config.loader import load_config
from scrapeyard.config.schema import FailStrategy, OnEmptyAction, OutputFormat
from scrapeyard.engine.resilience import CircuitBreaker, CircuitOpenError, ResultValidator
from scrapeyard.engine.scraper import TargetResult, scrape_target
from scrapeyard.formatters.factory import get_formatter
from scrapeyard.formatters.json_fmt import format_json
from scrapeyard.formatters.markdown_fmt import format_markdown
from scrapeyard.models.job import ActionTaken, ErrorRecord, ErrorType, JobStatus
from scrapeyard.storage.protocols import ErrorStore, JobStore, ResultStore
from scrapeyard.webhook.dispatcher import WebhookDispatcher
from scrapeyard.webhook.payload import build_webhook_payload, should_fire

logger = logging.getLogger(__name__)

_OUTPUT_FORMAT_TO_SAVE: dict[OutputFormat, str] = {
    OutputFormat.json: "json",
    OutputFormat.markdown: "markdown",
    OutputFormat.html: "html",
    OutputFormat.json_markdown: "json+markdown",
}


async def scrape_task(
    job_id: str,
    config_yaml: str,
    *,
    run_id: str | None = None,
    job_store: JobStore,
    result_store: ResultStore,
    error_store: ErrorStore,
    circuit_breaker: CircuitBreaker,
    webhook_dispatcher: WebhookDispatcher | None = None,
) -> None:
    """Execute a complete scrape job.

    This is the top-level worker function that:

    1. Parses the YAML config.
    2. Updates job status to *running*.
    3. Iterates resolved targets, respecting concurrency / delay / rate limits.
    4. Applies circuit breaker per domain.
    5. Validates results.
    6. Formats output and saves via *result_store*.
    7. Logs errors via *error_store*.
    8. Updates final job status.
    """
    try:
        started_at = datetime.now(timezone.utc)
        config = load_config(config_yaml)
        job = await job_store.get_job(job_id)

        settings = get_settings()
        adaptive_dir = str(Path(settings.adaptive_dir) / config.project)
        if _should_skip_delivery(job, run_id, settings.workers_running_lease_seconds, started_at):
            logger.info("Skipping duplicate or superseded delivery for job_id=%s run_id=%s", job_id, run_id)
            return

        job = job.model_copy(update={
            "status": JobStatus.running,
            "updated_at": started_at,
        })
        await job_store.update_job(job)

        targets = config.resolved_targets()
        concurrency = config.execution.concurrency
        delay_between = config.execution.delay_between
        domain_rate_limit = config.execution.domain_rate_limit

        # Track last request time per domain for rate limiting.
        domain_last_request: dict[str, float] = {}
        all_results: list[TargetResult] = []
        all_errors: list[str] = []
        sem = asyncio.Semaphore(concurrency)
        validator = ResultValidator(config.validation)

        async def _apply_validation(
            target_cfg: Any,
            domain: str,
            adaptive: bool,
            result: TargetResult,
            *,
            attempt: int = 1,
        ) -> TargetResult:
            if result.status != "success":
                return result

            validation = validator.validate(result.data)
            if validation.passed:
                return result

            action = ActionTaken(validation.action.value)
            await _log_error(
                job_id,
                config.project,
                target_cfg.url,
                attempt,
                ErrorType.content_empty,
                None,
                target_cfg.fetcher.value,
                action,
                error_store,
            )

            if validation.action == OnEmptyAction.warn:
                logger.warning("Validation warning for %s: %s", target_cfg.url, validation.message)
                result.errors.append(validation.message)
                return result

            if validation.action == OnEmptyAction.skip:
                logger.info("Skipping invalid result for %s: %s", target_cfg.url, validation.message)
                return TargetResult(
                    url=target_cfg.url,
                    status="success",
                    data=[],
                    errors=[validation.message],
                    pages_scraped=result.pages_scraped,
                )

            if validation.action == OnEmptyAction.fail:
                logger.info("Failing target %s due to validation: %s", target_cfg.url, validation.message)
                return TargetResult(
                    url=target_cfg.url,
                    status="failed",
                    data=[],
                    errors=[validation.message],
                    pages_scraped=result.pages_scraped,
                )

            logger.info("Retrying target %s after validation failure: %s", target_cfg.url, validation.message)
            retry_result = await scrape_target(
                target_cfg,
                adaptive,
                config.retry,
                adaptive_dir=adaptive_dir,
            )
            if retry_result.status != "success":
                logger.info("Recording failure for domain %s after validation retry", domain)
                circuit_breaker.record_failure(domain)
                for _ in retry_result.errors:
                    await _log_error(
                        job_id,
                        config.project,
                        target_cfg.url,
                        attempt + 1,
                        ErrorType.http_error,
                        None,
                        target_cfg.fetcher.value,
                        ActionTaken.fail,
                        error_store,
                    )
                return retry_result

            circuit_breaker.record_success(domain)
            retry_validation = validator.validate(retry_result.data)
            if retry_validation.passed:
                return retry_result

            await _log_error(
                job_id,
                config.project,
                target_cfg.url,
                attempt + 1,
                ErrorType.content_empty,
                None,
                target_cfg.fetcher.value,
                ActionTaken.fail,
                error_store,
            )
            return TargetResult(
                url=target_cfg.url,
                status="failed",
                data=[],
                errors=[retry_validation.message],
                pages_scraped=retry_result.pages_scraped,
            )

        async def _process_target(target_cfg: Any) -> TargetResult:
            async with sem:
                domain = urlparse(target_cfg.url).netloc

                # Circuit breaker check.
                try:
                    circuit_breaker.check(domain)
                except CircuitOpenError as exc:
                    logger.info("Circuit breaker open for %s", domain)
                    tr = TargetResult(url=target_cfg.url, status="failed", errors=[str(exc)])
                    await _log_error(
                        job_id, config.project, target_cfg.url, 0,
                        ErrorType.network_error, None, "circuit_breaker",
                        ActionTaken.circuit_break, error_store,
                    )
                    return tr

                # Domain rate limiting.
                now = time.monotonic()
                last = domain_last_request.get(domain, 0.0)
                wait = domain_rate_limit - (now - last)
                if wait > 0:
                    await asyncio.sleep(wait)
                domain_last_request[domain] = time.monotonic()

                # Spec 6.1: adaptive defaults to True for scheduled jobs, False for on-demand.
                if config.adaptive is not None:
                    adaptive = config.adaptive
                else:
                    adaptive = config.schedule is not None
                logger.info(
                    "Scraping %s with fetcher=%s adaptive=%s",
                    target_cfg.url, target_cfg.fetcher.value, adaptive,
                )
                result = await scrape_target(target_cfg, adaptive, config.retry, adaptive_dir=adaptive_dir)

                if result.status == "success":
                    circuit_breaker.record_success(domain)
                    result = await _apply_validation(target_cfg, domain, adaptive, result)
                else:
                    logger.warning(
                        "Recording failure for domain %s type=%s status=%s detail=%s",
                        domain,
                        result.error_type.value if result.error_type else ErrorType.http_error.value,
                        result.http_status,
                        result.error_detail or "; ".join(result.errors) or "unknown error",
                    )
                    circuit_breaker.record_failure(domain)
                    for err_msg in result.errors or [result.error_detail or "unknown scrape failure"]:
                        await _log_error(
                            job_id, config.project, target_cfg.url, 1,
                            result.error_type or ErrorType.http_error,
                            result.http_status,
                            target_cfg.fetcher.value,
                            ActionTaken.fail,
                            error_store,
                            error_message=err_msg,
                        )

                return result

        # Process targets with delay_between staggering.
        tasks: list[asyncio.Task] = []
        for i, t in enumerate(targets):
            if i > 0 and delay_between > 0:
                await asyncio.sleep(delay_between)
            tasks.append(asyncio.create_task(_process_target(t)))

        for task in tasks:
            tr = await task
            all_results.append(tr)
            all_errors.extend(tr.errors)

        flat_data: list[dict[str, Any]] = []
        for tr in all_results:
            flat_data.extend(tr.data)

        # Determine final status based on fail_strategy.
        failed_count = sum(1 for r in all_results if r.status == "failed")
        fail_strategy = config.execution.fail_strategy

        if fail_strategy == FailStrategy.all_or_nothing:
            if failed_count > 0:
                final_status = JobStatus.failed
                flat_data.clear()  # Discard all results.
            else:
                final_status = JobStatus.complete
        elif fail_strategy == FailStrategy.continue_:
            if flat_data:
                final_status = JobStatus.complete
            else:
                final_status = JobStatus.failed
        else:
            # FailStrategy.partial (default / current behavior).
            if failed_count == len(all_results) or not flat_data:
                final_status = JobStatus.failed
            elif failed_count > 0:
                final_status = JobStatus.partial
            else:
                final_status = JobStatus.complete

        # Format and save results if we have data.
        save_meta = None
        if flat_data:
            latest_job = await job_store.get_job(job_id)
            if _run_superseded(latest_job, run_id):
                logger.info("Skipping result save for superseded job_id=%s run_id=%s", job_id, run_id)
                return
            fmt = config.output.format
            group_by = config.output.group_by
            job_meta = {
                "project": config.project,
                "name": config.name,
                "job_id": job_id,
                "status": final_status.value,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "errors": all_errors,
            }
            formatted_results = [
                {
                    "url": tr.url,
                    "status": tr.status,
                    "data": tr.data[0] if len(tr.data) == 1 else tr.data,
                }
                for tr in all_results if tr.data
            ]

            save_fmt = _OUTPUT_FORMAT_TO_SAVE[fmt]

            if fmt == OutputFormat.json_markdown:
                json_output = format_json(job_meta, formatted_results, group_by)
                markdown_output = format_markdown(job_meta, formatted_results, group_by)
                save_meta = await result_store.save_result(
                    job_id,
                    json_output,
                    save_fmt,
                    run_id=run_id,
                    record_count=len(flat_data),
                    file_contents={"results.md": markdown_output},
                )
            else:
                formatter = get_formatter(fmt)
                formatted = formatter(job_meta, formatted_results, group_by)
                save_meta = await result_store.save_result(
                    job_id,
                    formatted,
                    save_fmt,
                    run_id=run_id,
                    record_count=len(flat_data),
                )

        # Webhook dispatch (fire-and-forget).
        completed_at = datetime.now(timezone.utc)
        latest_job = await job_store.get_job(job_id)
        if _run_superseded(latest_job, run_id):
            logger.info("Skipping finalization for superseded job_id=%s run_id=%s", job_id, run_id)
            return

        if webhook_dispatcher is not None and config.webhook is not None and should_fire(config.webhook, final_status):
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
            asyncio.create_task(webhook_dispatcher.dispatch(config.webhook, payload))

        # Update job status.
        job = await job_store.get_job(job_id)
        job = job.model_copy(update={
            "status": final_status,
            "updated_at": completed_at,
            "last_run_at": completed_at,
            "run_count": job.run_count + 1,
            "current_run_id": run_id,
        })
        await job_store.update_job(job)
    except Exception:
        logger.exception("scrape_task crashed for job_id=%s", job_id)
        try:
            job = await job_store.get_job(job_id)
            job = job.model_copy(update={
                "status": JobStatus.failed,
                "updated_at": datetime.now(timezone.utc),
            })
            await job_store.update_job(job)
        except Exception:
            logger.exception("Failed to mark job %s as failed", job_id)


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

    lease_age = (now - job.updated_at).total_seconds()
    return lease_age < running_lease_seconds


async def _log_error(
    job_id: str,
    project: str,
    url: str,
    attempt: int,
    error_type: ErrorType,
    http_status: int | None,
    fetcher_used: str,
    action: ActionTaken,
    error_store: ErrorStore,
    error_message: str | None = None,
) -> None:
    """Helper to log a structured error record."""
    record = ErrorRecord(
        job_id=job_id,
        project=project,
        target_url=url,
        attempt=attempt,
        error_type=error_type,
        http_status=http_status,
        fetcher_used=fetcher_used,
        error_message=error_message,
        action_taken=action,
    )
    await error_store.log_error(record)
