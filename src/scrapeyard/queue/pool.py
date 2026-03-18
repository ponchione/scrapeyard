"""Redis-backed queue service with embedded arq worker execution."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from arq.connections import ArqRedis, RedisSettings, create_pool
from arq.jobs import Job
from arq.worker import Worker, func

from scrapeyard.common.settings import get_settings

_ARQ_FUNCTION_NAME = "scrape_job"
_KEEP_RESULT_SECONDS = 3600
_PRIORITY_OFFSETS_MS = {
    "high": -1000,
    "normal": 0,
    "low": 1000,
}


class QueueJobHandle(Protocol):
    """Minimal async handle for waiting on queued job completion."""

    async def result(
        self,
        timeout: float | None = None,
        *,
        poll_delay: float = 0.5,
    ) -> Any: ...


class WorkerPool:
    """Queue service that enqueues jobs into Redis and executes them via arq."""

    def __init__(
        self,
        max_concurrent: int,
        max_browsers: int,
        memory_limit_mb: int,
        redis_settings: RedisSettings,
        queue_name: str,
        task_handler: Any = None,
    ) -> None:
        self._max_concurrent = max_concurrent
        self._max_browsers = max_browsers
        self._memory_limit_mb = memory_limit_mb
        self._redis_settings = redis_settings
        self._queue_name = queue_name
        self._task_handler = task_handler

        self._browser_semaphore = asyncio.Semaphore(max_browsers)
        self._redis: ArqRedis | None = None
        self._worker: Worker | None = None
        self._runner_task: asyncio.Task[None] | None = None
        self._active_tasks = 0
        self._active_browsers = 0
        self._started = False

    def _check_memory(self) -> bool:
        """Return True if current RSS is within limits."""
        if self._memory_limit_mb <= 0:
            return True
        try:
            statm = Path("/proc/self/statm").read_text()
            rss_pages = int(statm.split()[1])
            page_size = 4096
            rss_mb = (rss_pages * page_size) / (1024 * 1024)
        except (OSError, IndexError, ValueError):
            return True
        return rss_mb < self._memory_limit_mb

    def can_accept(self) -> bool:
        """Return True if the service can accept new work."""
        return self._check_memory()

    async def start(self) -> None:
        """Start the Redis connection and embedded arq worker."""
        if self._started:
            return

        self._redis = await create_pool(
            self._redis_settings,
            default_queue_name=self._queue_name,
        )
        self._worker = Worker(
            functions=[func(self._run_job, name=_ARQ_FUNCTION_NAME, keep_result=_KEEP_RESULT_SECONDS)],
            redis_pool=self._redis,
            queue_name=self._queue_name,
            handle_signals=False,
            max_jobs=self._max_concurrent,
            keep_result=_KEEP_RESULT_SECONDS,
            retry_jobs=False,
        )
        self._runner_task = asyncio.create_task(
            self._worker.async_run(),
            name="scrapeyard-arq-worker",
        )
        self._started = True

    async def stop(self) -> None:
        """Stop picking new jobs and close the embedded worker."""
        if not self._started:
            return

        assert self._worker is not None

        self._worker.allow_pick_jobs = False
        grace_seconds = get_settings().workers_shutdown_grace_seconds
        pending = [
            task for task in self._worker.tasks.values()
            if not task.done()
        ]
        if pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=grace_seconds,
                )
            except asyncio.TimeoutError:
                for task in pending:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

        if self._worker.main_task is not None:
            self._worker.main_task.cancel()
        if self._runner_task is not None:
            try:
                await self._runner_task
            except asyncio.CancelledError:
                pass

        await self._worker.close()

        self._redis = None
        self._worker = None
        self._runner_task = None
        self._started = False

    async def enqueue(
        self,
        job_id: str,
        config_yaml: str,
        priority: str = "normal",
        needs_browser: bool = False,
        *,
        run_id: str | None = None,
    ) -> QueueJobHandle:
        """Enqueue a scrape job and return a handle for awaiting completion."""
        if not self._check_memory():
            raise MemoryError(
                f"Process memory exceeds {self._memory_limit_mb}MB limit — rejecting task"
            )
        if not self._started:
            await self.start()

        assert self._redis is not None
        offset_ms = _PRIORITY_OFFSETS_MS[priority]
        defer_until = datetime.now(timezone.utc) + timedelta(milliseconds=offset_ms)
        queued = await self._redis.enqueue_job(
            _ARQ_FUNCTION_NAME,
            job_id,
            config_yaml,
            run_id,
            needs_browser=needs_browser,
            _job_id=run_id,
            _queue_name=self._queue_name,
            _defer_until=defer_until,
        )
        if queued is None:
            assert run_id is not None
            return Job(run_id, redis=self._redis, _queue_name=self._queue_name)
        return queued

    @property
    def active_tasks(self) -> int:
        return self._active_tasks

    @property
    def active_browsers(self) -> int:
        return self._active_browsers

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    @property
    def max_browsers(self) -> int:
        return self._max_browsers

    async def _run_job(
        self,
        _ctx: dict[str, Any],
        job_id: str,
        config_yaml: str,
        run_id: str | None = None,
        *,
        needs_browser: bool = False,
    ) -> dict[str, str]:
        """Execute one queued scrape job under browser-concurrency limits."""
        self._active_tasks += 1
        try:
            if needs_browser:
                async with self._browser_semaphore:
                    self._active_browsers += 1
                    try:
                        await self._execute(job_id, config_yaml, run_id=run_id)
                    finally:
                        self._active_browsers -= 1
            else:
                await self._execute(job_id, config_yaml, run_id=run_id)
        finally:
            self._active_tasks -= 1
        return {"job_id": job_id}

    async def _execute(self, job_id: str, config_yaml: str, *, run_id: str | None = None) -> None:
        if self._task_handler is not None:
            await self._task_handler(job_id, config_yaml, run_id=run_id)
