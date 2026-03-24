"""FastAPI dependency functions for storage, queue, and resilience instances."""

from __future__ import annotations

from functools import lru_cache

from arq.connections import RedisSettings

from scrapeyard.common.settings import get_settings
from scrapeyard.engine.rate_limiter import (
    DomainRateLimiter,
    LocalDomainRateLimiter,
    RedisDomainRateLimiter,
)
from scrapeyard.engine.resilience import CircuitBreaker
from scrapeyard.queue.pool import WorkerPool
from scrapeyard.queue.worker import scrape_task
from scrapeyard.scheduler.cron import SchedulerService
from scrapeyard.storage.error_store import SQLiteErrorStore
from scrapeyard.storage.job_store import SQLiteJobStore
from scrapeyard.storage.result_store import LocalResultStore
from scrapeyard.webhook.dispatcher import HttpWebhookDispatcher


@lru_cache(maxsize=1)
def get_job_store() -> SQLiteJobStore:
    return SQLiteJobStore()


@lru_cache(maxsize=1)
def get_error_store() -> SQLiteErrorStore:
    return SQLiteErrorStore()


@lru_cache(maxsize=1)
def get_result_store() -> LocalResultStore:
    settings = get_settings()
    job_store = get_job_store()

    async def _lookup(job_id: str) -> tuple[str, str]:
        job = await job_store.get_job(job_id)
        return job.project, job.name

    return LocalResultStore(settings.storage_results_dir, _lookup)


@lru_cache(maxsize=1)
def get_circuit_breaker() -> CircuitBreaker:
    settings = get_settings()
    return CircuitBreaker(
        max_consecutive_failures=settings.circuit_breaker_max_failures,
        cooldown_seconds=settings.circuit_breaker_cooldown_seconds,
    )


@lru_cache(maxsize=1)
def get_webhook_dispatcher() -> HttpWebhookDispatcher:
    return HttpWebhookDispatcher()


# Rate limiter — set during lifespan startup, not @lru_cache, because
# RedisDomainRateLimiter needs an async Redis connection that isn't
# available at import time.
_rate_limiter: DomainRateLimiter | None = None


def init_rate_limiter(redis: object | None = None) -> DomainRateLimiter:
    """Create and store the rate limiter singleton.

    Called from lifespan after Redis is available. When
    domain_rate_limit_shared is True and a redis handle is provided,
    returns RedisDomainRateLimiter; otherwise LocalDomainRateLimiter.
    """
    global _rate_limiter
    settings = get_settings()
    if settings.domain_rate_limit_shared and redis is not None:
        _rate_limiter = RedisDomainRateLimiter(redis)
    else:
        _rate_limiter = LocalDomainRateLimiter()
    return _rate_limiter


def get_rate_limiter() -> DomainRateLimiter:
    """Return the rate limiter singleton. Must call init_rate_limiter() first."""
    if _rate_limiter is None:
        return init_rate_limiter()
    return _rate_limiter


def reset_rate_limiter() -> None:
    """Reset the rate limiter singleton (for test teardown)."""
    global _rate_limiter
    _rate_limiter = None


@lru_cache(maxsize=1)
def get_worker_pool() -> WorkerPool:
    settings = get_settings()
    job_store = get_job_store()
    result_store = get_result_store()
    error_store = get_error_store()
    circuit_breaker = get_circuit_breaker()

    webhook_dispatcher = get_webhook_dispatcher()
    rate_limiter = get_rate_limiter()

    async def _task_handler(
        job_id: str,
        config_yaml: str,
        *,
        run_id: str | None = None,
        trigger: str = "adhoc",
    ) -> None:
        await scrape_task(
            job_id,
            config_yaml,
            run_id=run_id,
            trigger=trigger,
            job_store=job_store,
            result_store=result_store,
            error_store=error_store,
            circuit_breaker=circuit_breaker,
            rate_limiter=get_rate_limiter(),
            webhook_dispatcher=webhook_dispatcher,
        )

    return WorkerPool(
        max_concurrent=settings.workers_max_concurrent,
        max_browsers=settings.workers_max_browsers,
        memory_limit_mb=settings.workers_memory_limit_mb,
        redis_settings=RedisSettings.from_dsn(settings.redis_dsn),
        queue_name=settings.queue_name,
        task_handler=_task_handler,
    )


@lru_cache(maxsize=1)
def get_scheduler() -> SchedulerService:
    settings = get_settings()
    return SchedulerService(
        worker_pool=get_worker_pool(),
        job_store=get_job_store(),
        jitter_max_seconds=settings.scheduler_jitter_max_seconds,
    )
