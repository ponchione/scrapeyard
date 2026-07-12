"""Tests for hard async shutdown deadlines."""

from __future__ import annotations

import asyncio

import pytest

from scrapeyard.common.async_tools import MonotonicDeadline, await_with_timeout


@pytest.mark.asyncio
async def test_timeout_does_not_wait_for_cancellation_acknowledgement() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()

    async def cancellation_resistant() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()

    task = asyncio.create_task(cancellation_resistant())
    await started.wait()
    loop = asyncio.get_running_loop()
    before = loop.time()

    with pytest.raises(asyncio.TimeoutError):
        await await_with_timeout(task, timeout=0.005)

    assert loop.time() - before < 0.1
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    release.set()
    await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_deadline_budget_is_shared_between_phases() -> None:
    deadline = MonotonicDeadline(0.03)

    await deadline.run(asyncio.sleep(0.02))

    with pytest.raises(asyncio.TimeoutError):
        await deadline.run(asyncio.sleep(0.02))
    assert deadline.remaining == 0
