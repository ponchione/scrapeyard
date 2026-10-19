"""Bounded, owned executor for run-scoped blocking work."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
from typing import TYPE_CHECKING, Any, Callable, TypeVar

from scrapeyard.common.async_tools import consume_result_when_done, shield
from scrapeyard.runtime.metrics import ACTIVE_WORK, WORK_CAPACITY

if TYPE_CHECKING:
    from scrapeyard.common.budgets import RunBudget


T = TypeVar("T")


class OwnedThreadPool:
    """Bound submissions and retain capacity until underlying threads quiesce.

    Cancelling the awaiting coroutine never releases a slot early: the work
    counts as lingering until its thread returns. *label* names the
    capacity and active-work metrics.
    """

    def __init__(
        self,
        max_workers: int,
        *,
        label: str,
        thread_name_prefix: str,
        capacity_error: str,
    ) -> None:
        if max_workers < 1:
            raise ValueError(capacity_error)
        self.max_workers = max_workers
        self._label = label
        self._lingering_label = f"lingering_{label}"
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=thread_name_prefix,
        )
        self._slots = asyncio.Semaphore(max_workers)
        self._state_lock = threading.Lock()
        self._active = 0
        self._lingering = 0
        WORK_CAPACITY.labels(label).set(max_workers)
        ACTIVE_WORK.labels(label).set(0)
        ACTIVE_WORK.labels(self._lingering_label).set(0)

    @property
    def active(self) -> int:
        with self._state_lock:
            return self._active

    @property
    def lingering(self) -> int:
        with self._state_lock:
            return self._lingering

    async def _run_owned(
        self,
        submit: Callable[[ThreadPoolExecutor], Future[T]],
        *,
        on_cancel: Callable[[], None] | None = None,
    ) -> T:
        """Await submitted work without releasing its slot on cancellation."""

        await self._slots.acquire()
        loop = asyncio.get_running_loop()
        lingering = False
        try:
            future = submit(self._executor)
        except BaseException:
            self._slots.release()
            raise

        with self._state_lock:
            self._active += 1
            ACTIVE_WORK.labels(self._label).set(self._active)

        def release_slot(completed: Future[T]) -> None:
            del completed
            with self._state_lock:
                self._active -= 1
                if lingering:
                    self._lingering -= 1
                ACTIVE_WORK.labels(self._label).set(self._active)
                ACTIVE_WORK.labels(self._lingering_label).set(self._lingering)
            try:
                loop.call_soon_threadsafe(self._slots.release)
            except RuntimeError:
                # The event loop is already closed. Executor ownership still
                # lasted until the underlying future finished.
                pass

        future.add_done_callback(release_slot)
        wrapped = asyncio.wrap_future(future, loop=loop)
        # A cancelled caller leaves the work running with nobody to hear it fail.
        consume_result_when_done(wrapped)
        try:
            return await shield(wrapped)
        except asyncio.CancelledError:
            if on_cancel is not None:
                on_cancel()
            with self._state_lock:
                if not future.done():
                    lingering = True
                    self._lingering += 1
                    ACTIVE_WORK.labels(self._lingering_label).set(self._lingering)
            raise

    def shutdown(self) -> None:
        """Wait for owned work and reject future submissions."""

        self._executor.shutdown(wait=True, cancel_futures=True)


class RunThreadPool(OwnedThreadPool):
    """Owned executor for run-scoped blocking work."""

    def __init__(self, max_workers: int) -> None:
        super().__init__(
            max_workers,
            label="run_threads",
            thread_name_prefix="scrapeyard-run",
            capacity_error="Run thread worker capacity must be positive",
        )

    async def run(
        self,
        function: Callable[..., T],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        """Run blocking work without releasing its slot on coroutine cancellation."""

        return await self._run_owned(
            lambda executor: executor.submit(copy_context().run, function, *args, **kwargs)
        )


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
