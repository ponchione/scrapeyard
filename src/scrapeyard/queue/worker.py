"""Stateless scrape task: orchestrates fetch → validate → store."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from scrapeyard.common.settings import get_settings
from scrapeyard.config.loader import load_config
from scrapeyard.config.schema import FailStrategy, GroupBy, OnEmptyAction
from scrapeyard.engine.proxy import redact_proxy_url, resolve_proxy
from scrapeyard.engine.rate_limiter import DomainRateLimiter
from scrapeyard.engine.resilience import CircuitBreaker, CircuitOpenError, ResultValidator
from scrapeyard.engine.scraper import TargetResult, scrape_target
from scrapeyard.models.job import ActionTaken, ErrorRecord, ErrorType, JobStatus
from scrapeyard.storage.database import get_db
from scrapeyard.storage.protocols import ErrorStore, JobStore, ResultStore
from scrapeyard.webhook.dispatcher import WebhookDispatcher
from scrapeyard.webhook.payload import build_webhook_payload, should_fire

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
        adaptive_dir = str(Path(settings.adaptive_dir) / config.project)
        if _should_skip_delivery(job, run_id, settings.workers_running_lease_seconds, started_at):
            logger.info("Skipping duplicate or superseded delivery for job_id=%s run_id=%s", job_id, run_id)
            return

        job = job.model_copy(update={
            "status": JobStatus.running,
            "updated_at": started_at,
        })
        await job_store.update_job(job)

        if run_id is not None:
            config_hash = hashlib.sha256(
                config_yaml.encode()
            ).hexdigest()
            async with get_db("jobs.db") as db:
                await db.execute(
                    """INSERT INTO job_runs
                       (run_id, job_id, status, trigger,
                        config_hash, started_at)
                       VALUES (?, ?, 'running', ?, ?, ?)""",
                    (
                        run_id, job_id, trigger,
                        config_hash,
                        started_at.isoformat(),
                    ),
                )
                await db.commit()

        targets = config.resolved_targets()
        concurrency = config.execution.concurrency
        delay_between = config.execution.delay_between
        domain_rate_limit = config.execution.domain_rate_limit

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
            proxy_url: str | None = None,
        ) -> TargetResult:
            if result.status != "success":
                return result

            validation = validator.validate(result.data)
            if validation.passed:
                return result

            action = ActionTaken(validation.action.value)
            await _log_error(
                job_id,
                run_id or "",
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
                proxy_url=proxy_url,
            )
            if retry_result.status != "success":
                logger.info("Recording failure for domain %s after validation retry", domain)
                circuit_breaker.record_failure(domain)
                for _ in retry_result.errors:
                    await _log_error(
                        job_id,
                        run_id or "",
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
                run_id or "",
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
                        job_id, run_id or "", config.project,
                        target_cfg.url, 0,
                        ErrorType.network_error, None, "circuit_breaker",
                        ActionTaken.circuit_break, error_store,
                    )
                    return tr

                # Domain rate limiting (shared across jobs when Redis-backed).
                await rate_limiter.acquire(domain, domain_rate_limit)

                # Spec 6.1: adaptive defaults to True for scheduled jobs, False for on-demand.
                if config.adaptive is not None:
                    adaptive = config.adaptive
                else:
                    adaptive = config.schedule is not None

                # Resolve proxy for this target.
                proxy_url = resolve_proxy(target_cfg, config.proxy, settings.proxy_url)

                if proxy_url:
                    logger.info(
                        "Scraping %s with fetcher=%s adaptive=%s proxy=%s",
                        target_cfg.url, target_cfg.fetcher.value, adaptive,
                        redact_proxy_url(proxy_url),
                    )
                else:
                    logger.info(
                        "Scraping %s with fetcher=%s adaptive=%s",
                        target_cfg.url, target_cfg.fetcher.value, adaptive,
                    )
                result = await scrape_target(
                    target_cfg, adaptive, config.retry,
                    adaptive_dir=adaptive_dir, proxy_url=proxy_url,
                )

                if result.status == "success":
                    circuit_breaker.record_success(domain)
                    result = await _apply_validation(
                        target_cfg, domain, adaptive, result, proxy_url=proxy_url,
                    )
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
                            job_id, run_id or "", config.project,
                            target_cfg.url, 1,
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
                logger.info(
                    "Skipping result save for superseded job_id=%s run_id=%s",
                    job_id, run_id,
                )
                return
            job_meta = {
                "project": config.project,
                "name": config.name,
                "job_id": job_id,
                "status": final_status.value,
                "completed_at": datetime.now(
                    timezone.utc
                ).isoformat(),
                "errors": all_errors,
            }
            group_by = config.output.group_by

            if group_by == GroupBy.merge:
                merged: list[Any] = []
                for tr in all_results:
                    for item in tr.data:
                        if isinstance(item, dict):
                            item["_source"] = urlparse(
                                tr.url
                            ).netloc
                        merged.append(item)
                output_data = {**job_meta, "results": merged}
            else:
                grouped: dict[str, Any] = {}
                for tr in all_results:
                    if not tr.data:
                        continue
                    domain = urlparse(tr.url).netloc
                    grouped[domain] = {
                        "status": tr.status,
                        "count": len(tr.data),
                        "data": tr.data,
                    }
                output_data = {
                    **job_meta, "results": grouped,
                }
            save_meta = await result_store.save_result(
                job_id,
                output_data,
                run_id=run_id,
                status=final_status.value,
                record_count=len(flat_data),
            )

        # Finalize run row.
        if run_id is not None:
            async with get_db("errors.db") as db:
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM errors "
                    "WHERE run_id = ?",
                    (run_id,),
                )
                error_count = (await cursor.fetchone())[0]

            async with get_db("jobs.db") as db:
                await db.execute(
                    """UPDATE job_runs
                       SET status = ?, completed_at = ?,
                           record_count = ?, error_count = ?
                       WHERE run_id = ?""",
                    (
                        final_status.value,
                        datetime.now(timezone.utc).isoformat(),
                        len(flat_data),
                        error_count,
                        run_id,
                    ),
                )
                await db.commit()

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
        if run_id is not None:
            try:
                async with get_db("jobs.db") as db:
                    await db.execute(
                        """UPDATE job_runs
                           SET status = 'failed',
                               completed_at = ?
                           WHERE run_id = ?
                             AND status = 'running'""",
                        (
                            datetime.now(
                                timezone.utc
                            ).isoformat(),
                            run_id,
                        ),
                    )
                    await db.commit()
            except Exception:
                logger.exception(
                    "Failed to finalize run %s", run_id,
                )


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
    run_id: str,
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
    await error_store.log_error(record)
