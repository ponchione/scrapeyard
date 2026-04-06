"""Tests for scrapeyard.queue.pool lifecycle and public behavior."""

from __future__ import annotations

import asyncio
from typing import Any
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from arq.connections import RedisSettings

from scrapeyard.queue.pool import WorkerPool


class _FakeWorkerTask:
    def __init__(self, done: bool = False) -> None:
        self._done = done
        self.cancelled = False

    def done(self) -> bool:
        return self._done

    def cancel(self) -> None:
        self.cancelled = True
        self._done = True


def _make_pool(**overrides: Any) -> WorkerPool:
    defaults: dict[str, Any] = {
        "max_concurrent": 4,
        "max_browsers": 2,
        "memory_limit_mb": 0,
        "redis_settings": RedisSettings(host="localhost"),
        "queue_name": "test-queue",
    }
    defaults.update(overrides)
    return WorkerPool(**defaults)


def test_can_accept_returns_true_when_limit_disabled():
    pool = _make_pool(memory_limit_mb=0)
    assert pool.can_accept() is True


def test_can_accept_returns_true_when_limit_negative():
    pool = _make_pool(memory_limit_mb=-1)
    assert pool.can_accept() is True


def test_can_accept_returns_true_on_oserror():
    pool = _make_pool(memory_limit_mb=512)
    with patch("scrapeyard.queue.pool.Path.read_text", side_effect=OSError("no proc")):
        assert pool.can_accept() is True


def test_can_accept_returns_false_when_over_limit():
    pool = _make_pool(memory_limit_mb=100)
    with (
        patch("scrapeyard.queue.pool.Path.read_text", return_value="50000 30000 1000 500 0 2000 0"),
        patch("scrapeyard.queue.pool.os.sysconf", return_value=4096),
    ):
        assert pool.can_accept() is False


def test_can_accept_returns_true_when_under_limit():
    pool = _make_pool(memory_limit_mb=500)
    with (
        patch("scrapeyard.queue.pool.Path.read_text", return_value="50000 10000 1000 500 0 2000 0"),
        patch("scrapeyard.queue.pool.os.sysconf", return_value=4096),
    ):
        assert pool.can_accept() is True


def test_properties_return_correct_values():
    pool = _make_pool(max_concurrent=8, max_browsers=3)
    assert pool.max_concurrent == 8
    assert pool.max_browsers == 3
    assert pool.active_tasks == 0
    assert pool.active_browsers == 0
    assert pool.redis is None


@pytest.mark.asyncio
async def test_start_initializes_redis_and_worker_once():
    pool = _make_pool()
    fake_redis = MagicMock()
    fake_worker = MagicMock()
    fake_worker.async_run = AsyncMock()

    def _create_task(coro, *_args, **_kwargs):
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        return MagicMock()

    with (
        patch("scrapeyard.queue.pool.create_pool", new=AsyncMock(return_value=fake_redis)) as create_pool_mock,
        patch("scrapeyard.queue.pool.Worker", return_value=fake_worker) as worker_cls,
        patch("scrapeyard.queue.pool.asyncio.create_task", side_effect=_create_task) as create_task_mock,
    ):
        await pool.start()
        await pool.start()

    create_pool_mock.assert_awaited_once()
    worker_cls.assert_called_once()
    create_task_mock.assert_called_once()
    assert pool.redis is fake_redis


@pytest.mark.asyncio
async def test_stop_raises_when_started_without_worker():
    pool = _make_pool()
    pool._started = True
    pool._worker = None

    with pytest.raises(RuntimeError, match="never started"):
        await pool.stop()


@pytest.mark.asyncio
async def test_stop_waits_for_pending_tasks_and_closes_worker(monkeypatch):
    pool = _make_pool()
    pending_task = asyncio.create_task(asyncio.sleep(0))
    runner_task = asyncio.create_task(asyncio.sleep(0))
    close_calls = 0

    async def _close() -> None:
        nonlocal close_calls
        close_calls += 1

    worker = SimpleNamespace(
        allow_pick_jobs=True,
        tasks={"one": pending_task},
        main_task=None,
        close=_close,
    )
    pool._started = True
    pool._worker = worker
    pool._runner_task = runner_task
    monkeypatch.setattr(
        "scrapeyard.queue.pool.get_settings",
        lambda: MagicMock(workers_shutdown_grace_seconds=1),
    )

    await pool.stop()

    assert worker.allow_pick_jobs is False
    assert close_calls == 1
    assert pool.redis is None
    assert pool._worker is None
    assert pool._runner_task is None
    assert pool._started is False


@pytest.mark.asyncio
async def test_stop_cancels_pending_tasks_after_timeout(monkeypatch):
    pool = _make_pool()
    pending = _FakeWorkerTask()
    close_calls = 0

    async def _close() -> None:
        nonlocal close_calls
        close_calls += 1

    worker = SimpleNamespace(
        allow_pick_jobs=True,
        tasks={"one": pending},
        main_task=None,
        close=_close,
    )
    runner_task = asyncio.create_task(asyncio.sleep(0))
    pool._started = True
    pool._worker = worker
    pool._runner_task = runner_task
    monkeypatch.setattr(
        "scrapeyard.queue.pool.get_settings",
        lambda: MagicMock(workers_shutdown_grace_seconds=1),
    )

    async def _timeout(awaitable, *_args, **_kwargs):
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        raise asyncio.TimeoutError

    async def _gather(*_args, **_kwargs):
        return [None]

    monkeypatch.setattr("scrapeyard.queue.pool.asyncio.wait_for", _timeout)
    monkeypatch.setattr("scrapeyard.queue.pool.asyncio.gather", _gather)

    await pool.stop()

    assert pending.cancelled is True
    assert close_calls == 1


@pytest.mark.asyncio
async def test_enqueue_raises_memory_error_when_pool_cannot_accept():
    pool = _make_pool(memory_limit_mb=1)
    with patch.object(pool, "can_accept", return_value=False):
        with pytest.raises(MemoryError, match="memory exceeds"):
            await pool.enqueue("job-1", "config: yaml")


@pytest.mark.asyncio
async def test_enqueue_starts_pool_and_enqueues_job():
    pool = _make_pool()
    queued_job = MagicMock()
    fake_redis = MagicMock(enqueue_job=AsyncMock(return_value=queued_job))

    async def _fake_start() -> None:
        pool._started = True
        pool._redis = fake_redis

    with (
        patch.object(pool, "can_accept", return_value=True),
        patch.object(pool, "start", _fake_start),
    ):
        result = await pool.enqueue("job-1", "config: yaml", priority="high", run_id="run-1")

    assert result is queued_job
    fake_redis.enqueue_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_enqueue_returns_job_handle_when_enqueue_returns_none():
    pool = _make_pool()
    fake_redis = MagicMock(enqueue_job=AsyncMock(return_value=None))
    pool._started = True
    pool._redis = fake_redis

    with patch.object(pool, "can_accept", return_value=True):
        result = await pool.enqueue("job-1", "config: yaml", run_id="run-1")

    assert result is not None
    assert hasattr(result, "result")


@pytest.mark.asyncio
async def test_enqueue_requires_run_id_when_job_lookup_fallback_needed():
    pool = _make_pool()
    pool._started = True
    pool._redis = MagicMock(enqueue_job=AsyncMock(return_value=None))

    with patch.object(pool, "can_accept", return_value=True):
        with pytest.raises(RuntimeError, match="no run_id"):
            await pool.enqueue("job-1", "config: yaml")


@pytest.mark.asyncio
async def test_run_job_tracks_active_browser_and_calls_execute():
    pool = _make_pool(max_browsers=1)
    pool._execute = AsyncMock()

    result = await pool._run_job({}, "job-1", "config: yaml", run_id="run-1", needs_browser=True)

    pool._execute.assert_awaited_once_with(
        "job-1",
        "config: yaml",
        run_id="run-1",
        trigger="adhoc",
    )
    assert pool.active_tasks == 0
    assert pool.active_browsers == 0
    assert result == {"job_id": "job-1"}


@pytest.mark.asyncio
async def test_run_job_without_browser_skips_browser_counter():
    pool = _make_pool()
    pool._execute = AsyncMock()

    await pool._run_job({}, "job-1", "config: yaml", trigger="scheduled")

    pool._execute.assert_awaited_once_with(
        "job-1",
        "config: yaml",
        run_id=None,
        trigger="scheduled",
    )
    assert pool.active_tasks == 0
    assert pool.active_browsers == 0


@pytest.mark.asyncio
async def test_execute_delegates_to_task_handler():
    handler = AsyncMock()
    pool = _make_pool(task_handler=handler)

    await pool._execute("job-1", "config: yaml", run_id="run-1", trigger="scheduled")

    handler.assert_awaited_once_with(
        "job-1",
        "config: yaml",
        run_id="run-1",
        trigger="scheduled",
    )
