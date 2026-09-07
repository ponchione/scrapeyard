"""Cancellation-safe helpers for enforcing one monotonic async deadline."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from contextlib import suppress
from typing import Any, TypeVar


T = TypeVar("T")


async def await_cleanup(awaitable: Awaitable[T]) -> T:
    """Retain resource ownership until cleanup finishes, even on repeated cancels."""
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


class AwaitableCancelled(Exception):
    """An inner awaitable ended by cancellation without cancelling its caller."""


def _consume_task_result(task: asyncio.Future[Any]) -> None:
    """Retrieve a late task exception without ever blocking its caller."""

    if task.cancelled():
        return
    with suppress(Exception, asyncio.CancelledError):
        task.result()


def cancel_task_nowait(task: asyncio.Future[Any]) -> None:
    """Request cancellation and consume the eventual result without blocking."""

    task.cancel()
    task.add_done_callback(_consume_task_result)


async def await_with_timeout(
    awaitable: Awaitable[T],
    *,
    timeout: float | None,
) -> T:
    """Await within *timeout* without waiting for cancellation acknowledgement.

    ``asyncio.wait_for`` can exceed its timeout indefinitely when the inner task
    suppresses cancellation. Shutdown code needs a hard wall-clock ceiling, so
    this helper requests cancellation and returns control immediately instead.
    A zero timeout still gives an immediately-completing cleanup one event-loop
    turn to run.
    """

    task = asyncio.ensure_future(awaitable)
    try:
        if timeout is None:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.cancelled():
                    raise AwaitableCancelled from None
                raise
        if timeout > 0:
            done, _pending = await asyncio.wait({task}, timeout=timeout)
            if task in done:
                if task.cancelled():
                    raise AwaitableCancelled
                return task.result()
        else:
            await asyncio.sleep(0)
            if task.done():
                if task.cancelled():
                    raise AwaitableCancelled
                return task.result()
        cancel_task_nowait(task)
        raise asyncio.TimeoutError
    except asyncio.CancelledError:
        cancel_task_nowait(task)
        raise


class MonotonicDeadline:
    """One event-loop deadline shared by sequential shutdown phases."""

    def __init__(self, timeout: float | None) -> None:
        self._loop = asyncio.get_running_loop()
        self._deadline = (
            None
            if timeout is None
            else self._loop.time() + max(0.0, timeout)
        )

    @property
    def remaining(self) -> float | None:
        if self._deadline is None:
            return None
        return max(0.0, self._deadline - self._loop.time())

    async def run(self, awaitable: Awaitable[T]) -> T:
        return await await_with_timeout(awaitable, timeout=self.remaining)
