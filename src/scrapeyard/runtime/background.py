"""Health supervision for interval-driven background loops."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from scrapeyard.runtime.metrics import RECONCILIATION_PASSES, mark_last_success


class BackgroundLoopMonitor:
    """Track pass outcomes and detect a live loop that has stopped succeeding."""

    def __init__(
        self,
        name: str,
        *,
        interval_seconds: float,
        grace_cycles: float = 2.0,
        failure_grace_count: int = 1,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if grace_cycles <= 1:
            raise ValueError("grace_cycles must allow at least one failed cycle")
        if failure_grace_count < 0:
            raise ValueError("failure_grace_count must not be negative")
        self.name = name
        self.interval_seconds = interval_seconds
        self._grace_seconds = interval_seconds * grace_cycles
        self._failure_grace_count = failure_grace_count
        self._clock = clock
        self._started_at = self._now()
        self._last_success_at: float | None = None
        self._last_failure_at: float | None = None
        self._last_failure_type: str | None = None
        self._consecutive_failures = 0
        self._task: asyncio.Task[None] | None = None

    def _now(self) -> float:
        return self._clock()

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def last_success_at(self) -> float | None:
        return self._last_success_at

    @property
    def last_failure_at(self) -> float | None:
        return self._last_failure_at

    def bind(self, task: asyncio.Task[None]) -> None:
        self._task = task

    def record_success(self) -> None:
        self._last_success_at = self._now()
        self._consecutive_failures = 0
        RECONCILIATION_PASSES.labels(self.name, "success").inc()
        mark_last_success(self.name)

    def record_failure(self, exc: BaseException) -> None:
        self._last_failure_at = self._now()
        self._last_failure_type = type(exc).__name__
        self._consecutive_failures += 1
        RECONCILIATION_PASSES.labels(self.name, "failure").inc()

    @property
    def background_ok(self) -> bool:
        task = self._task
        if task is None or task.done():
            return False
        if self._consecutive_failures > self._failure_grace_count:
            return False
        last_healthy = self._last_success_at or self._started_at
        return self._now() - last_healthy <= self._grace_seconds

    @property
    def background_detail(self) -> str | None:
        if self.background_ok:
            return None
        task = self._task
        if task is None:
            return f"{self.name} task missing"
        if task.cancelled():
            return f"{self.name} task stopped"
        if task.done():
            exception = task.exception()
            return (
                f"{self.name} task stopped"
                if exception is None
                else f"{self.name} task failed: {type(exception).__name__}"
            )
        failure = self._last_failure_type or "no successful pass"
        return (
            f"{self.name} is outside its success/failure grace; "
            f"consecutive_failures={self._consecutive_failures} "
            f"last_failure={failure} success_grace_seconds={self._grace_seconds:g}"
        )
