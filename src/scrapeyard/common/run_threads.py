"""Bounded, owned executor for run-scoped blocking work."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from typing import TYPE_CHECKING, Any, Callable, TypeVar

from scrapeyard.runtime.metrics import ACTIVE_WORK, WORK_CAPACITY

if TYPE_CHECKING:
    from scrapeyard.common.budgets import RunBudget


T = TypeVar("T")


class RunThreadPool:
    """Bound submissions and retain capacity until underlying threads quiesce."""

    def __init__(self, max_workers: int) -> None:
        if max_workers < 1:
            raise ValueError("Run thread worker capacity must be positive")
        self.max_workers = max_workers
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="scrapeyard-run",
        )
        self._slots = asyncio.Semaphore(max_workers)
        self._state_lock = threading.Lock()
        self._active = 0
        self._lingering = 0
        WORK_CAPACITY.labels("run_threads").set(max_workers)
        ACTIVE_WORK.labels("run_threads").set(0)
        ACTIVE_WORK.labels("lingering_run_threads").set(0)

    @property
    def active(self) -> int:
        with self._state_lock:
            return self._active

    @property
    def lingering(self) -> int:
        with self._state_lock:
            return self._lingering

    async def run(
        self,
        function: Callable[..., T],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        """Run blocking work without releasing its slot on coroutine cancellation."""

        await self._slots.acquire()
        loop = asyncio.get_running_loop()
        lingering = False
        try:
            future = self._executor.submit(partial(function, *args, **kwargs))
        except BaseException:
            self._slots.release()
            raise

        with self._state_lock:
            self._active += 1
            ACTIVE_WORK.labels("run_threads").set(self._active)

        def release_slot(completed: Future[T]) -> None:
            del completed
            with self._state_lock:
                self._active -= 1
                if lingering:
                    self._lingering -= 1
                ACTIVE_WORK.labels("run_threads").set(self._active)
                ACTIVE_WORK.labels("lingering_run_threads").set(self._lingering)
            try:
                loop.call_soon_threadsafe(self._slots.release)
            except RuntimeError:
                # The event loop is already closed. Executor ownership still
                # lasted until the underlying future finished.
                pass

        future.add_done_callback(release_slot)
        wrapped = asyncio.wrap_future(future, loop=loop)
        try:
            return await asyncio.shield(wrapped)
        except asyncio.CancelledError:
            with self._state_lock:
                if not future.done():
                    lingering = True
                    self._lingering += 1
                    ACTIVE_WORK.labels("lingering_run_threads").set(self._lingering)
            raise

    def shutdown(self) -> None:
        """Wait for owned work and reject future submissions."""

        self._executor.shutdown(wait=True, cancel_futures=True)


async def run_thread_work(
    function: Callable[..., T],
    *args: Any,
    run_budget: RunBudget | None,
    pool: RunThreadPool | None = None,
    **kwargs: Any,
) -> T:
    """Run one blocking operation under the run deadline and owned executor."""

    if pool is None:
        from scrapeyard.api.dependencies import get_run_thread_pool

        pool = get_run_thread_pool()
    work = pool.run(function, *args, **kwargs)
    if run_budget is None:
        return await work
    return await run_budget.wait_for(work)
