"""FastAPI dependency functions for storage, queue, and resilience instances."""

from __future__ import annotations

from functools import lru_cache

from scrapeyard.common.settings import get_settings
from scrapeyard.engine.resilience import CircuitBreaker
from scrapeyard.queue.pool import WorkerPool
from scrapeyard.queue.worker import scrape_task
from scrapeyard.scheduler.cron import SchedulerService
from scrapeyard.storage.error_store import SQLiteErrorStore
from scrapeyard.storage.job_store import SQLiteJobStore
from scrapeyard.storage.result_store import LocalResultStore


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
        return (job.project, job.name)

    return LocalResultStore(settings.storage_results_dir, _lookup)


@lru_cache(maxsize=1)
def get_circuit_breaker() -> CircuitBreaker:
    settings = get_settings()
    return CircuitBreaker(
        max_consecutive_failures=settings.circuit_breaker_max_failures,
        cooldown_seconds=settings.circuit_breaker_cooldown_seconds,
    )


@lru_cache(maxsize=1)
def get_worker_pool() -> WorkerPool:
    settings = get_settings()
    job_store = get_job_store()
    result_store = get_result_store()
    error_store = get_error_store()
    circuit_breaker = get_circuit_breaker()

    async def _task_handler(job_id: str, config_yaml: str) -> None:
        await scrape_task(
            job_id,
            config_yaml,
            job_store=job_store,
            result_store=result_store,
            error_store=error_store,
            circuit_breaker=circuit_breaker,
        )

    return WorkerPool(
        max_concurrent=settings.workers_max_concurrent,
        max_browsers=settings.workers_max_browsers,
        memory_limit_mb=settings.workers_memory_limit_mb,
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
