"""Concurrent per-target fetch, pacing, validation, and error flushing."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from scrapeyard.common.budgets import BudgetExceeded
from scrapeyard.common.qualification import qualification_checkpoint
from scrapeyard.config.schema import FetcherType, TargetConfig
from scrapeyard.engine.browser_pool import BrowserPool, activate_browser_pool
from scrapeyard.engine.domain_guard import activate_domain_policy, domain_guard_host
from scrapeyard.engine.fetch_classifier import ACCESS_DENIAL_TYPES, classify_fetch_exception
from scrapeyard.engine.page_cache import activate_page_cache
from scrapeyard.engine.rate_limiter import DomainRateLimiter
from scrapeyard.engine.resilience import CircuitBreaker, CircuitState, ResultValidator
from scrapeyard.engine.scrape_models import SELF_IMPOSED_ERROR_TYPES
from scrapeyard.engine.scraper import TargetResult, TargetStatus
from scrapeyard.engine.url_guard import redact_userinfo_in_url, url_host_label
from scrapeyard.models.job import ErrorRecord
from scrapeyard.queue.browser_limiter import BrowserExecutionLimiter
from scrapeyard.queue.cancellation import RunActivityGuard
from scrapeyard.queue.error_records import TargetErrorRecorder
from scrapeyard.queue.target_execution import (
    TargetRuntimeContext,
    guard_target_execution,
    log_target_fetch,
    record_failed_target,
    resolve_target_runtime_context,
)
from scrapeyard.queue.validation_policy import apply_validation
from scrapeyard.queue.worker_context import JobExecutionContext, TargetProcessingContext
from scrapeyard.queue.worker_recovery import exception_detail
from scrapeyard.runtime.metrics import active_target, observe_target
from scrapeyard.storage.protocols import ErrorStore
from scrapeyard.storage.types import RunOwnershipError

logger = logging.getLogger(__name__)

ScrapeTargetFn = Callable[..., Awaitable[TargetResult]]


async def process_all_targets(
    *,
    context: JobExecutionContext,
    job_id: str,
    run_id: str | None,
    circuit_breaker: CircuitBreaker,
    rate_limiter: DomainRateLimiter,
    browser_limiter: BrowserExecutionLimiter | None,
    error_store: ErrorStore,
    scrape: ScrapeTargetFn,
    monotonic: Callable[[], float],
) -> list[TargetResult]:
    """Dispatch all targets with concurrency, delay, and rate limiting."""
    config = context.config
    targets = list(config.resolved_targets())
    sem = asyncio.Semaphore(config.execution.concurrency)
    start_lock = asyncio.Lock()
    next_start_at = 0.0
    denied_hosts: set[str] = set()
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
        replay=context.replaying,
    )
    pacing = not context.replaying

    async def _process_one(target_index: int, target_cfg: TargetConfig) -> TargetResult:
        nonlocal next_start_at
        pending_errors: list[ErrorRecord] = []
        cancelled = False
        target_result: TargetResult | None = None
        target_started = 0.0
        try:
            async with sem:
                await context.activity.checkpoint("before_target_fetch")
                if pacing and config.execution.delay_between > 0:
                    async with start_lock:
                        wait_seconds = max(0.0, next_start_at - monotonic())
                        if wait_seconds > 0:
                            await context.budget.sleep(wait_seconds)
                            await context.activity.checkpoint("after_target_delay")
                        context.budget.check_deadline()
                        next_start_at = monotonic() + config.execution.delay_between
                context.budget.check_deadline()
                host = domain_guard_host(target_cfg.url)
                if host in denied_hosts:
                    detail = (
                        "Not attempted: an earlier target on this host was denied or challenged"
                    )
                    return TargetResult(
                        url=target_cfg.url,
                        status=TargetStatus.failed,
                        errors=[detail],
                        error_detail=detail,
                    )
                target_started = monotonic()
                with active_target():
                    target_result = await fetch_and_validate_target(
                        target_cfg=target_cfg,
                        target_index=target_index,
                        context=target_context,
                        pending_errors=pending_errors,
                        scrape=scrape,
                    )
                if (
                    target_result.error_type in ACCESS_DENIAL_TYPES
                    or target_result.http_status in {401, 403, 407}
                ):
                    denied_hosts.add(host)
                    # 407 is a proxy credential failure, not the site denying access.
                    if (
                        context.domain_policy is not None
                        and not context.replaying
                        and target_result.http_status != 407
                    ):
                        await context.domain_policy.record_denial(target_cfg.url)
                if (
                    pacing
                    and config.execution.post_target_delay > 0
                    and target_index < len(targets) - 1
                    and circuit_breaker.state(url_host_label(target_cfg.url))
                    is not CircuitState.open
                ):
                    await context.budget.sleep(config.execution.post_target_delay)
                    await context.activity.checkpoint("after_post_target_delay")
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
                    duration_seconds=monotonic() - target_started,
                )
            if not cancelled:
                await flush_errors(
                    error_store,
                    pending_errors,
                    activity=context.activity,
                )

    # Replay launches no browser, so there is nothing to share.
    browser_pool = (
        BrowserPool(config.execution.concurrency)
        if config.execution.reuse_browser and not context.replaying
        else None
    )
    # Target tasks inherit the run's domain policy, page cache and browser pool.
    with (
        activate_domain_policy(context.domain_policy),
        activate_page_cache(context.page_cache, unavailable=context.page_cache_unavailable),
        activate_browser_pool(browser_pool),
    ):
        tasks = [
            asyncio.create_task(_process_one(target_index, target_cfg))
            for target_index, target_cfg in enumerate(targets)
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
            if browser_pool is not None:
                try:
                    await browser_pool.aclose()
                except Exception as exc:
                    # Results are already extracted; a failed close must not lose them.
                    logger.warning(
                        "Closing shared browsers failed job_id=%s run_id=%s error_type=%s",
                        job_id,
                        run_id,
                        type(exc).__name__,
                    )


async def fetch_and_validate_target(
    *,
    target_cfg: TargetConfig,
    context: TargetProcessingContext,
    pending_errors: list[ErrorRecord],
    scrape: ScrapeTargetFn,
    target_index: int = 0,
) -> TargetResult:
    """Fetch a single target and run validation. Returns the result."""
    recorder = context.recorder(pending_errors)
    runtime = resolve_target_runtime_context(
        target_cfg=target_cfg,
        config=context.config,
        settings=context.settings,
        run_artifacts_dir=context.run_artifacts_dir,
        target_index=target_index,
        proxy_session=context.proxy_session,
    )
    circuit_open = (
        None
        if context.replay
        else await guard_target_execution(
            runtime=runtime,
            target_cfg=target_cfg,
            circuit_breaker=context.circuit_breaker,
            recorder=recorder,
        )
    )
    if circuit_open is not None:
        return TargetResult(
            url=target_cfg.url,
            status=TargetStatus.failed,
            errors=[str(circuit_open)],
        )

    log_target_fetch(target_cfg, runtime)
    try:
        qualification_checkpoint("during_target_execution")
        if target_cfg.fetcher in (FetcherType.dynamic, FetcherType.stealthy) and not context.replay:
            if context.browser_limiter is None:
                return await _scrape_and_validate_target(
                    target_cfg,
                    context,
                    runtime,
                    recorder,
                    scrape=scrape,
                    target_index=target_index,
                )
            async with context.browser_limiter.slot():
                return await _scrape_and_validate_target(
                    target_cfg,
                    context,
                    runtime,
                    recorder,
                    scrape=scrape,
                    target_index=target_index,
                )
        return await _scrape_and_validate_target(
            target_cfg,
            context,
            runtime,
            recorder,
            scrape=scrape,
            target_index=target_index,
        )
    except asyncio.CancelledError:
        context.circuit_breaker.abort_probe(runtime.domain, runtime.circuit_probe)
        runtime.circuit_probe = None
        raise
    except RunOwnershipError:
        context.circuit_breaker.abort_probe(runtime.domain, runtime.circuit_probe)
        runtime.circuit_probe = None
        raise
    except BudgetExceeded:
        if runtime.upstream_response_observed:
            recorder.record_success(runtime.domain, probe=runtime.circuit_probe)
        else:
            context.circuit_breaker.abort_probe(
                runtime.domain,
                runtime.circuit_probe,
            )
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
    *,
    scrape: ScrapeTargetFn,
    target_index: int,
) -> TargetResult:
    result = await scrape(
        target_cfg,
        runtime.adaptive,
        context.config.retry,
        adaptive_dir=context.adaptive_dir,
        proxy_url=runtime.proxy_url,
        artifacts_dir=runtime.artifacts_dir,
        budget=context.budget,
        cancellation_guard=context.activity.checkpoint,
        response_observer=runtime.mark_upstream_response,
        rate_limiter=context.rate_limiter,
        domain_rate_limit=context.config.execution.domain_rate_limit,
    )
    await context.activity.checkpoint("after_target_fetch")
    if not result.is_success:
        if context.replay or result.error_type in SELF_IMPOSED_ERROR_TYPES:
            # Nothing reached the site: keep the error record, not a circuit failure.
            context.circuit_breaker.abort_probe(runtime.domain, runtime.circuit_probe)
            runtime.circuit_probe = None
            recorder.record_uncounted_failure(
                target_url=target_cfg.url,
                fetcher_used=target_cfg.fetcher.value,
                result=result,
            )
            return result
        record_failed_target(
            runtime=runtime,
            result=result,
            target_cfg=target_cfg,
            recorder=recorder,
        )
        return result

    if not context.replay:
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
        scrape=scrape,
        proxy_url=runtime.proxy_url,
        target_index=target_index,
        budget=context.budget,
        cancellation_guard=context.activity.checkpoint,
        replay=context.replay,
    )


def _target_exception_result(
    *,
    runtime: TargetRuntimeContext,
    target_cfg: TargetConfig,
    recorder: TargetErrorRecorder,
    exc: Exception,
) -> TargetResult:
    error_type, http_status, debug = classify_fetch_exception(exc, target_cfg.fetcher)
    detail = exception_detail(exc)
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


async def flush_errors(
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
