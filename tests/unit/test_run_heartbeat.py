"""Time-controlled tests for the per-run heartbeat supervisor."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from scrapeyard.queue.heartbeat import RunHeartbeat
from scrapeyard.storage.types import RunOwnershipError


class FakeClock:
    def __init__(self) -> None:
        self.monotonic_value = 0.0
        self.utc_value = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
        self.ticks: asyncio.Queue[None] = asyncio.Queue()

    def monotonic(self) -> float:
        return self.monotonic_value

    def utc_now(self) -> datetime:
        return self.utc_value

    async def wait(self, _delay: float) -> None:
        await self.ticks.get()

    async def tick(self, seconds: float) -> None:
        self.monotonic_value += seconds
        self.utc_value += timedelta(seconds=seconds)
        self.ticks.put_nowait(None)
        await asyncio.sleep(0)
        await asyncio.sleep(0)


def _heartbeat(store: AsyncMock, clock: FakeClock, *, timeout: float = 90) -> RunHeartbeat:
    return RunHeartbeat(
        job_store=store,
        job_id="job-1",
        run_id="run-1",
        last_success_at=clock.utc_value,
        interval_seconds=30,
        timeout_seconds=timeout,
        monotonic=clock.monotonic,
        utc_clock=clock.utc_now,
        wait=clock.wait,
    )


async def test_run_spanning_multiple_intervals_remains_owned() -> None:
    store = AsyncMock()
    clock = FakeClock()
    heartbeat = _heartbeat(store, clock)
    heartbeat.start(owner_task=asyncio.current_task())

    await clock.tick(30)
    await clock.tick(30)
    await clock.tick(30)

    assert store.heartbeat_run.await_count == 3
    assert heartbeat.ownership_lost is False
    assert heartbeat.last_success_at == clock.utc_value
    await heartbeat.stop()
    assert heartbeat.task is None


async def test_transient_write_failure_retries_without_abandoning(caplog) -> None:
    store = AsyncMock()
    store.heartbeat_run.side_effect = [OSError("busy"), None]
    clock = FakeClock()
    heartbeat = _heartbeat(store, clock)
    heartbeat.start(owner_task=asyncio.current_task())

    await clock.tick(30)
    assert heartbeat.failure_count == 1
    assert heartbeat.ownership_lost is False
    assert "job_id=job-1" in caplog.text
    assert "run_id=run-1" in caplog.text
    assert "last_heartbeat=" in caplog.text
    assert "failure_count=1" in caplog.text
    assert "timeout_seconds=90" in caplog.text
    await clock.tick(30)

    assert store.heartbeat_run.await_count == 2
    assert heartbeat.failure_count == 0
    assert heartbeat.ownership_lost is False
    await heartbeat.stop()


async def test_sustained_write_failure_stops_and_cancels_owner() -> None:
    store = AsyncMock()
    store.heartbeat_run.side_effect = OSError("disk unavailable")
    clock = FakeClock()
    owner = asyncio.create_task(asyncio.Event().wait())
    heartbeat = _heartbeat(store, clock)
    heartbeat.start(owner_task=owner)

    await clock.tick(30)
    await clock.tick(30)
    await clock.tick(30)

    assert heartbeat.ownership_lost is True
    assert heartbeat.failure_count == 3
    assert heartbeat.task is not None and heartbeat.task.done()
    assert owner.cancelled() or owner.cancelling()
    await asyncio.gather(owner, return_exceptions=True)
    await heartbeat.stop()


async def test_storage_ownership_rejection_is_immediate() -> None:
    store = AsyncMock()
    store.heartbeat_run.side_effect = RunOwnershipError(
        "heartbeat",
        "job-1",
        "run-1",
    )
    clock = FakeClock()
    owner = asyncio.create_task(asyncio.Event().wait())
    heartbeat = _heartbeat(store, clock)
    heartbeat.start(owner_task=owner)

    await clock.tick(30)

    assert heartbeat.ownership_lost is True
    assert heartbeat.failure_count == 0
    assert owner.cancelled() or owner.cancelling()
    await asyncio.gather(owner, return_exceptions=True)
    await heartbeat.stop()


async def test_stop_cancels_pending_wait_without_extra_write() -> None:
    store = AsyncMock()
    clock = FakeClock()
    heartbeat = _heartbeat(store, clock)
    heartbeat.start(owner_task=asyncio.current_task())

    await heartbeat.stop()

    store.heartbeat_run.assert_not_awaited()
    assert heartbeat.task is None


async def test_stop_swallows_cleanup_failure() -> None:
    async def _raise() -> None:
        raise RuntimeError("cleanup failed")

    store = AsyncMock()
    clock = FakeClock()
    heartbeat = _heartbeat(store, clock)
    heartbeat._task = asyncio.create_task(_raise())
    await asyncio.sleep(0)

    await heartbeat.stop()

    assert heartbeat.task is None
