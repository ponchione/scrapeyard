"""Process-wide concurrency control for browser target execution."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator


class BrowserExecutionLimiter:
    """Limit active browser targets and expose their current activity."""

    def __init__(self, max_concurrent: int) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        self._max_concurrent = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._active = 0

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
        self._active += 1
        try:
            yield
        finally:
            self._active -= 1
            self._semaphore.release()
