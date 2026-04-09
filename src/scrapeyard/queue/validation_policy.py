"""Validation policy helpers for worker target results."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Awaitable, Callable

from scrapeyard.config.schema import OnEmptyAction, ScrapeConfig, TargetConfig
from scrapeyard.engine.resilience import CircuitBreaker, ResultValidator
from scrapeyard.engine.scraper import TargetResult, TargetStatus
from scrapeyard.models.job import ActionTaken, ErrorRecord, ErrorType
from scrapeyard.queue.error_records import build_error_record, validation_error_type

logger = logging.getLogger(__name__)

ScrapeCallable = Callable[..., Awaitable[TargetResult]]


async def apply_validation(
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
    scrape: ScrapeCallable,
    proxy_url: str | None = None,
    attempt: int = 1,
) -> TargetResult:
    """Validate a successful result; retry once on validation failure."""
    if result.status is not TargetStatus.success:
        return result

    validation = validator.validate(result.data)
    if validation.passed:
        return result

    pending_errors.append(build_error_record(
        job_id,
        run_id or "",
        config.project,
        target_cfg.url,
        attempt,
        validation_error_type(result),
        None,
        target_cfg.fetcher.value,
        ActionTaken(validation.action.value),
        error_message=validation.message,
    ))

    if validation.action == OnEmptyAction.warn:
        logger.warning("Validation warning for %s: %s", target_cfg.url, validation.message)
        result.errors.append(validation.message)
        return result

    if validation.action == OnEmptyAction.skip:
        logger.info("Skipping invalid result for %s: %s", target_cfg.url, validation.message)
        return TargetResult(
            url=target_cfg.url,
            status=TargetStatus.success,
            data=[],
            errors=[validation.message],
            pages_scraped=result.pages_scraped,
            debug=result.debug,
        )

    if validation.action == OnEmptyAction.fail:
        logger.info("Failing target %s due to validation: %s", target_cfg.url, validation.message)
        return TargetResult(
            url=target_cfg.url,
            status=TargetStatus.failed,
            data=[],
            errors=[validation.message],
            pages_scraped=result.pages_scraped,
            error_type=validation_error_type(result),
            error_detail=validation.message,
            debug=result.debug,
        )

    logger.info("Retrying target %s after validation failure: %s", target_cfg.url, validation.message)
    retry_result = await scrape(
        target_cfg,
        adaptive,
        config.retry,
        adaptive_dir=adaptive_dir,
        proxy_url=proxy_url,
        artifacts_dir=_build_retry_artifacts_dir(run_artifacts_dir, domain),
    )
    if retry_result.status is not TargetStatus.success:
        logger.info("Recording failure for domain %s after validation retry", domain)
        circuit_breaker.record_failure(domain)
        for _ in retry_result.errors:
            pending_errors.append(build_error_record(
                job_id,
                run_id or "",
                config.project,
                target_cfg.url,
                attempt + 1,
                retry_result.error_type or ErrorType.http_error,
                retry_result.http_status,
                target_cfg.fetcher.value,
                ActionTaken.fail,
                error_message=retry_result.error_detail or "; ".join(retry_result.errors),
            ))
        return retry_result

    circuit_breaker.record_success(domain)
    retry_validation = validator.validate(retry_result.data)
    if retry_validation.passed:
        return retry_result

    pending_errors.append(build_error_record(
        job_id,
        run_id or "",
        config.project,
        target_cfg.url,
        attempt + 1,
        validation_error_type(retry_result),
        None,
        target_cfg.fetcher.value,
        ActionTaken.fail,
        error_message=retry_validation.message,
    ))
    return TargetResult(
        url=target_cfg.url,
        status=TargetStatus.failed,
        data=[],
        errors=[retry_validation.message],
        pages_scraped=retry_result.pages_scraped,
        error_type=validation_error_type(retry_result),
        error_detail=retry_validation.message,
        debug=retry_result.debug,
    )


def _build_retry_artifacts_dir(run_artifacts_dir: str | None, domain: str) -> str | None:
    return None if run_artifacts_dir is None else str(Path(run_artifacts_dir) / domain)
