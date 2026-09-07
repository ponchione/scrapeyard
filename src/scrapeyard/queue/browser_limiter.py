"""Process-wide concurrency control for browser target execution."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from scrapeyard.queue.memory import memory_headroom_mb
from scrapeyard.runtime.metrics import MEMORY_DEFERRALS


class BrowserExecutionLimiter:
    """Limit active browser targets and expose their current activity."""

    def __init__(
        self, max_concurrent: int, *, memory_limit_mb: int = 0, memory_reserve_mb: int = 0,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        self._max_concurrent = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._active = 0
        self._memory_limit_mb = memory_limit_mb
        self._memory_reserve_mb = memory_reserve_mb

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    @property
    def active(self) -> int:
        return self._active

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Hold one browser-target permit until the surrounding work exits."""
        await self._semaphore.acquire()
        try:
            # No await between the check and increment: concurrent starts
            # reserve headroom even before browser RSS becomes visible.
            while memory_headroom_mb(self._memory_limit_mb) <= (self._active + 1) * self._memory_reserve_mb:
                MEMORY_DEFERRALS.labels("browser").inc()
                await asyncio.sleep(1)
            self._active += 1
            try:
                yield
            finally:
                self._active -= 1
        finally:
            self._semaphore.release()
