"""Target fetch orchestration helpers for the worker."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scrapeyard.common.paths import safe_path_part
from scrapeyard.config.schema import ScrapeConfig, TargetConfig
from scrapeyard.engine.proxy import redact_proxy_url, resolve_proxy
from scrapeyard.engine.rate_limiter import DomainRateLimiter
from scrapeyard.engine.resilience import CircuitBreaker, CircuitOpenError
from scrapeyard.engine.scraper import TargetResult
from scrapeyard.engine.url_guard import redact_userinfo_in_text, redact_userinfo_in_url, url_host_label
from scrapeyard.models.job import ActionTaken, ErrorType
from scrapeyard.queue.error_records import TargetErrorRecorder

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TargetRuntimeContext:
    domain: str
    adaptive: bool
    proxy_url: str | None
    artifacts_dir: str | None


def resolve_target_runtime_context(
    *,
    target_cfg: TargetConfig,
    config: ScrapeConfig,
    settings: Any,
    run_artifacts_dir: str | None,
) -> TargetRuntimeContext:
    domain = url_host_label(target_cfg.url)
    adaptive = config.adaptive if config.adaptive is not None else config.schedule is not None
    proxy_url = resolve_proxy(target_cfg, config.proxy, settings.proxy_url)
    artifacts_dir = None if run_artifacts_dir is None else str(
        Path(run_artifacts_dir) / safe_path_part(domain, label="target domain")
    )
    return TargetRuntimeContext(
        domain=domain,
        adaptive=adaptive,
        proxy_url=proxy_url,
        artifacts_dir=artifacts_dir,
    )


async def guard_target_execution(
    *,
    runtime: TargetRuntimeContext,
    config: ScrapeConfig,
    target_cfg: TargetConfig,
    circuit_breaker: CircuitBreaker,
    rate_limiter: DomainRateLimiter,
    recorder: TargetErrorRecorder,
) -> CircuitOpenError | None:
    try:
        circuit_breaker.check(runtime.domain)
    except CircuitOpenError as exc:
        logger.info("Circuit breaker open for %s", runtime.domain)
        recorder.record_circuit_break(target_cfg.url)
        return exc

    await rate_limiter.acquire(runtime.domain, config.execution.domain_rate_limit)
    return None


def log_target_fetch(target_cfg: TargetConfig, runtime: TargetRuntimeContext) -> None:
    if runtime.proxy_url:
        logger.info(
            "Scraping %s with fetcher=%s adaptive=%s proxy=%s",
            redact_userinfo_in_url(target_cfg.url),
            target_cfg.fetcher.value,
            runtime.adaptive,
            redact_proxy_url(runtime.proxy_url),
        )
    else:
        logger.info(
            "Scraping %s with fetcher=%s adaptive=%s",
            redact_userinfo_in_url(target_cfg.url),
            target_cfg.fetcher.value,
            runtime.adaptive,
        )


def record_failed_target(
    *,
    runtime: TargetRuntimeContext,
    result: TargetResult,
    target_cfg: TargetConfig,
    recorder: TargetErrorRecorder,
) -> None:
    logger.warning(
        "Recording failure for domain %s type=%s status=%s detail=%s",
        runtime.domain,
        result.error_type.value if result.error_type else ErrorType.http_error.value,
        result.http_status,
        redact_userinfo_in_text(result.error_detail or "; ".join(result.errors) or "unknown error"),
    )
    recorder.record_target_failure(
        domain=runtime.domain,
        target_url=target_cfg.url,
        fetcher_used=target_cfg.fetcher.value,
        action=ActionTaken.fail,
        result=result,
    )
