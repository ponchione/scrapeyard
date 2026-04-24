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

    _record_validation_failure(
        pending_errors=pending_errors,
        job_id=job_id,
        run_id=run_id,
        config=config,
        target_cfg=target_cfg,
        attempt=attempt,
        result=result,
        action=ActionTaken(validation.action.value),
        message=validation.message,
    )

    if validation.action == OnEmptyAction.warn:
        return _handle_warn_action(result, target_cfg, validation.message)
    if validation.action == OnEmptyAction.skip:
        return _handle_skip_action(result, target_cfg, validation.message)
    if validation.action == OnEmptyAction.fail:
        return _handle_fail_action(result, target_cfg, validation.message)

    logger.info("Retrying target %s after validation failure: %s", target_cfg.url, validation.message)
    return await _retry_after_validation_failure(
        target_cfg=target_cfg,
        domain=domain,
        adaptive=adaptive,
        pending_errors=pending_errors,
        config=config,
        adaptive_dir=adaptive_dir,
        run_artifacts_dir=run_artifacts_dir,
        job_id=job_id,
        run_id=run_id,
        circuit_breaker=circuit_breaker,
        validator=validator,
        scrape=scrape,
        proxy_url=proxy_url,
    )


def _record_validation_failure(
    *,
    pending_errors: list[ErrorRecord],
    job_id: str,
    run_id: str | None,
    config: ScrapeConfig,
    target_cfg: TargetConfig,
    attempt: int,
    result: TargetResult,
    action: ActionTaken,
    message: str,
) -> None:
    pending_errors.append(
        build_error_record(
            job_id,
            run_id or "",
            config.project,
            target_cfg.url,
            attempt,
            validation_error_type(result),
            None,
            target_cfg.fetcher.value,
            action,
            error_message=message,
        )
    )


def _handle_warn_action(result: TargetResult, target_cfg: TargetConfig, message: str) -> TargetResult:
    logger.warning("Validation warning for %s: %s", target_cfg.url, message)
    result.errors.append(message)
    return result


def _handle_skip_action(result: TargetResult, target_cfg: TargetConfig, message: str) -> TargetResult:
    logger.info("Skipping invalid result for %s: %s", target_cfg.url, message)
    return TargetResult(
        url=target_cfg.url,
        status=TargetStatus.success,
        data=[],
        errors=[message],
        pages_scraped=result.pages_scraped,
        debug=result.debug,
    )


def _handle_fail_action(result: TargetResult, target_cfg: TargetConfig, message: str) -> TargetResult:
    logger.info("Failing target %s due to validation: %s", target_cfg.url, message)
    return _build_validation_failed_result(target_cfg, result, message)


async def _retry_after_validation_failure(
    *,
    target_cfg: TargetConfig,
    domain: str,
    adaptive: bool,
    pending_errors: list[ErrorRecord],
    config: ScrapeConfig,
    adaptive_dir: str,
    run_artifacts_dir: str | None,
    job_id: str,
    run_id: str | None,
    circuit_breaker: CircuitBreaker,
    validator: ResultValidator,
    scrape: ScrapeCallable,
    proxy_url: str | None,
) -> TargetResult:
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
            pending_errors.append(
                build_error_record(
                    job_id,
                    run_id or "",
                    config.project,
                    target_cfg.url,
                    2,
                    retry_result.error_type or ErrorType.http_error,
                    retry_result.http_status,
                    target_cfg.fetcher.value,
                    ActionTaken.fail,
                    error_message=retry_result.error_detail or "; ".join(retry_result.errors),
                )
            )
        return retry_result

    circuit_breaker.record_success(domain)
    retry_validation = validator.validate(retry_result.data)
    if retry_validation.passed:
        return retry_result

    _record_validation_failure(
        pending_errors=pending_errors,
        job_id=job_id,
        run_id=run_id,
        config=config,
        target_cfg=target_cfg,
        attempt=2,
        result=retry_result,
        action=ActionTaken.fail,
        message=retry_validation.message,
    )
    return _build_validation_failed_result(target_cfg, retry_result, retry_validation.message)


def _build_validation_failed_result(
    target_cfg: TargetConfig,
    result: TargetResult,
    message: str,
) -> TargetResult:
    return TargetResult(
        url=target_cfg.url,
        status=TargetStatus.failed,
        data=[],
        errors=[message],
        pages_scraped=result.pages_scraped,
        error_type=validation_error_type(result),
        error_detail=message,
        debug=result.debug,
    )


def _build_retry_artifacts_dir(run_artifacts_dir: str | None, domain: str) -> str | None:
    return None if run_artifacts_dir is None else str(Path(run_artifacts_dir) / domain)
