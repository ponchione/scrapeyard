"""Worker pool with concurrency limits and priority-based enqueue."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any


class _Priority(IntEnum):
    """Numeric priority — lower value = higher priority."""

    high = 0
    normal = 1
    low = 2


@dataclass(order=True)
class _QueueItem:
    priority: int
    seq: int  # tie-breaker for FIFO within same priority
    job_id: str = field(compare=False)
    config_yaml: str = field(compare=False)
    needs_browser: bool = field(default=False, compare=False)


class WorkerPool:
    """Manages concurrency limits and an in-memory priority queue.

    Parameters
    ----------
    max_concurrent:
        Maximum number of tasks running concurrently.
    max_browsers:
        Maximum number of browser-based tasks running concurrently.
    memory_limit_mb:
        Reject new tasks when RSS exceeds this threshold (0 = no limit).
    task_handler:
        Async callable ``(job_id, config_yaml) -> None`` invoked for each task.
    """

    def __init__(
        self,
        max_concurrent: int,
        max_browsers: int,
        memory_limit_mb: int,
        task_handler: Any = None,
    ) -> None:
        self._max_concurrent = max_concurrent
        self._max_browsers = max_browsers
        self._memory_limit_mb = memory_limit_mb
        self._task_handler = task_handler

        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._browser_semaphore = asyncio.Semaphore(max_browsers)
        self._queue: asyncio.PriorityQueue[_QueueItem] = asyncio.PriorityQueue()
        self._seq = 0
        self._running = False
        self._consumer_task: asyncio.Task | None = None
        self._active_tasks = 0

    def _check_memory(self) -> bool:
        """Return True if current RSS is within limits."""
        if self._memory_limit_mb <= 0:
            return True
        try:
            # /proc/self/statm fields are in pages; page size is typically 4096.
            statm = Path("/proc/self/statm").read_text()
            rss_pages = int(statm.split()[1])
            page_size = 4096
            rss_mb = (rss_pages * page_size) / (1024 * 1024)
        except (OSError, IndexError, ValueError):
            return True  # can't read — assume OK
        return rss_mb < self._memory_limit_mb

    def can_accept(self) -> bool:
        """Return True if the pool can accept a new task (memory + concurrency)."""
        if not self._check_memory():
            return False
        return self._active_tasks < self._max_concurrent

    async def enqueue(
        self,
        job_id: str,
        config_yaml: str,
        priority: str = "normal",
        needs_browser: bool = False,
    ) -> None:
        """Add a job to the priority queue.

        Raises
        ------
        MemoryError
            If process RSS exceeds ``memory_limit_mb``.
        """
        if not self._check_memory():
            raise MemoryError(
                f"Process memory exceeds {self._memory_limit_mb}MB limit — rejecting task"
            )
        prio = _Priority[priority]
        self._seq += 1
        item = _QueueItem(
            priority=prio, seq=self._seq, job_id=job_id,
            config_yaml=config_yaml, needs_browser=needs_browser,
        )
        await self._queue.put(item)

    async def start(self) -> None:
        """Start the consumer loop."""
        self._running = True
        self._consumer_task = asyncio.create_task(self._consume())

    async def stop(self) -> None:
        """Signal the consumer to stop and wait for it."""
        self._running = False
        if self._consumer_task is not None:
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass

    async def _consume(self) -> None:
        """Pull items from the queue and dispatch them under the semaphore."""
        while self._running:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            asyncio.create_task(self._run(item))

    @property
    def active_tasks(self) -> int:
        return self._active_tasks

    async def _run(self, item: _QueueItem) -> None:
        """Execute a single task under concurrency limits."""
        async with self._semaphore:
            if item.needs_browser:
                async with self._browser_semaphore:
                    await self._execute(item)
            else:
                await self._execute(item)

    async def _execute(self, item: _QueueItem) -> None:
        self._active_tasks += 1
        try:
            if self._task_handler is not None:
                await self._task_handler(item.job_id, item.config_yaml)
        finally:
            self._active_tasks -= 1
