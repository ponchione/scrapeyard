"""Tests for scrapeyard.queue.pool lifecycle and public behavior."""

from __future__ import annotations

import asyncio
from typing import Any
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from arq.connections import RedisSettings

from scrapeyard.queue.cancellation import QueueCancellationOutcome
from scrapeyard.queue.pool import (
    QueueDeliveryState,
    WorkerPool,
    _DeliverySnapshot,
)
from scrapeyard.queue.priority import WeightedPriorityPolicy


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


def test_check_memory_returns_true_when_limit_disabled():
    pool = _make_pool(memory_limit_mb=0)
    assert pool._check_memory() is True


def test_check_memory_returns_true_when_limit_negative():
    pool = _make_pool(memory_limit_mb=-1)
    assert pool._check_memory() is True


def test_check_memory_returns_true_on_oserror():
    pool = _make_pool(memory_limit_mb=512)
    with patch("scrapeyard.queue.memory.Path.read_text", side_effect=OSError("no proc")):
        assert pool._check_memory() is True


def test_check_memory_returns_false_when_over_limit():
    pool = _make_pool(memory_limit_mb=100)
    with (
        patch("scrapeyard.queue.memory.Path.read_text", return_value="50000 30000 1000 500 0 2000 0"),
        patch("scrapeyard.queue.memory.os.sysconf", return_value=4096),
    ):
        assert pool._check_memory() is False


def test_check_memory_returns_true_when_under_limit():
    pool = _make_pool(memory_limit_mb=500)
    with (
        patch("scrapeyard.queue.memory.Path.read_text", return_value="50000 10000 1000 500 0 2000 0"),
        patch("scrapeyard.queue.memory.os.sysconf", return_value=4096),
    ):
        assert pool._check_memory() is True


def test_properties_return_correct_values():
    pool = _make_pool(max_concurrent=8, max_browsers=3)
    assert pool.max_concurrent == 8
    assert pool.max_browsers == 3
    assert pool.active_tasks == 0
    assert pool.active_browsers == 0
    assert pool.redis is None


@pytest.mark.asyncio
async def test_ping_requires_started_redis_pool():
    pool = _make_pool()

    with pytest.raises(RuntimeError, match="redis pool not connected"):
        await pool.ping()


def _select_available(
    policy: WeightedPriorityPolicy,
    available: set[str],
) -> str:
    selected = next(
        priority
        for priority in policy.selection_order()
        if priority in available
    )
    policy.admitted()
    return selected


def test_weighted_policy_gives_normal_and_low_bounded_fair_turns():
    policy = WeightedPriorityPolicy()

    selected = [
        _select_available(policy, {"high", "normal", "low"})
        for _ in range(14)
    ]

    assert selected == [
        "high", "high", "high", "high", "normal", "normal", "low",
        "high", "high", "high", "high", "normal", "normal", "low",
    ]


def test_later_high_overtakes_waiting_normal_and_low_after_current_admission():
    policy = WeightedPriorityPolicy()
    assert _select_available(policy, {"normal", "low"}) == "normal"

    assert _select_available(policy, {"high", "normal", "low"}) == "high"


def test_weighted_policy_is_work_conserving_when_preferred_queue_is_empty():
    policy = WeightedPriorityPolicy()

    assert _select_available(policy, {"low"}) == "low"
    assert _select_available(policy, {"normal"}) == "normal"


@pytest.mark.asyncio
async def test_queue_depths_reports_only_priority_intake_members():
    pool = _make_pool()
    redis = MagicMock()
    pipeline = MagicMock(execute=AsyncMock(return_value=[2, 3, 5]))
    redis.pipeline.return_value.__aenter__.return_value = pipeline
    pool._redis = redis

    assert await pool.queue_depths() == {"high": 2, "normal": 3, "low": 5}
    assert [call.args[0] for call in pipeline.zcard.call_args_list] == [
        "test-queue:priority:high",
        "test-queue:priority:normal",
        "test-queue:priority:low",
    ]


@pytest.mark.asyncio
async def test_queue_operational_snapshot_is_constant_cost_and_reports_oldest_age(
    monkeypatch,
):
    pool = _make_pool()
    redis = MagicMock()
    pipeline = MagicMock(
        execute=AsyncMock(
            return_value=[
                2,
                [(b"old-high", 99_000.0)],
                0,
                [],
                1,
                [(b"future-low", 101_000.0)],
            ]
        )
    )
    redis.pipeline.return_value.__aenter__.return_value = pipeline
    pool._redis = redis
    monkeypatch.setattr("scrapeyard.queue.pool.timestamp_ms", lambda: 100_000)

    snapshot = await pool.queue_operational_snapshot()

    assert snapshot == {
        "high": (2, 1.0),
        "normal": (0, 0.0),
        "low": (1, 0.0),
    }
    assert pipeline.zcard.call_count == 3
    assert pipeline.zrange.call_count == 3


def test_worker_background_health_detects_stopped_runner():
    pool = _make_pool()
    pool._started = True
    pool._runner_task = MagicMock(
        done=MagicMock(return_value=True),
        cancelled=MagicMock(return_value=False),
        exception=MagicMock(return_value=RuntimeError("secret")),
    )

    assert pool.background_ok is False
    assert pool.background_detail == "worker runner task failed: RuntimeError"


@pytest.mark.asyncio
async def test_start_initializes_redis_and_worker_once(monkeypatch):
    pool = _make_pool(job_timeout_seconds=37.5)
    fake_redis = MagicMock()
    fake_worker = MagicMock()
    fake_worker.async_run = AsyncMock()
    wait_for_timeouts: list[float] = []

    async def _wait_for(awaitable, *, timeout):
        wait_for_timeouts.append(timeout)
        return await awaitable

    def _create_task(coro, *_args, **_kwargs):
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        return MagicMock()

    monkeypatch.setattr(
        "scrapeyard.queue.pool.get_settings",
        lambda: SimpleNamespace(workers_redis_connect_timeout_seconds=7.0),
    )
    monkeypatch.setattr("scrapeyard.queue.pool.asyncio.wait_for", _wait_for)

    with (
        patch("scrapeyard.queue.pool.create_pool", new=AsyncMock(return_value=fake_redis)) as create_pool_mock,
        patch("scrapeyard.queue.pool._PriorityWorker", return_value=fake_worker) as worker_cls,
        patch("scrapeyard.queue.pool.asyncio.create_task", side_effect=_create_task) as create_task_mock,
    ):
        await pool.start()
        await pool.start()

    create_pool_mock.assert_awaited_once()
    worker_cls.assert_called_once()
    assert worker_cls.call_args.kwargs["allow_abort_jobs"] is True
    assert worker_cls.call_args.kwargs["job_timeout"] == 37.5
    assert worker_cls.call_args.kwargs["priority_queues"] == {
        "high": "test-queue:priority:high",
        "normal": "test-queue:priority:normal",
        "low": "test-queue:priority:low",
    }
    create_task_mock.assert_called_once()
    assert wait_for_timeouts == [7.0]
    assert pool.redis is fake_redis


@pytest.mark.asyncio
async def test_start_raises_clear_error_on_redis_connect_timeout(monkeypatch, caplog):
    pool = _make_pool(queue_name="critical-queue")

    async def _timeout(awaitable, *, timeout):
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(
        "scrapeyard.queue.pool.get_settings",
        lambda: SimpleNamespace(workers_redis_connect_timeout_seconds=2.5),
    )
    monkeypatch.setattr("scrapeyard.queue.pool.asyncio.wait_for", _timeout)
    caplog.set_level("ERROR", logger="scrapeyard.queue.pool")

    with (
        patch("scrapeyard.queue.pool.create_pool", new=AsyncMock(return_value=MagicMock())) as create_pool_mock,
        patch("scrapeyard.queue.pool.Worker") as worker_cls,
        patch("scrapeyard.queue.pool.asyncio.create_task") as create_task_mock,
    ):
        with pytest.raises(RuntimeError, match="Timed out connecting to Redis after 2.5s"):
            await pool.start()

    create_pool_mock.assert_called_once()
    worker_cls.assert_not_called()
    create_task_mock.assert_not_called()
    assert "Timed out connecting to Redis queue critical-queue after 2.5s" in caplog.text
    assert pool.redis is None
    assert pool._worker is None
    assert pool._started is False


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
    pending = asyncio.create_task(asyncio.Event().wait())
    await asyncio.sleep(0)
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
        lambda: MagicMock(workers_shutdown_grace_seconds=0.001),
    )

    await pool.stop()

    assert pending.cancelled()
    assert close_calls == 1


@pytest.mark.asyncio
async def test_stop_cancellation_releases_active_browser_target(monkeypatch):
    browser_started = asyncio.Event()

    async def _task_handler(*_args, browser_limiter, **_kwargs) -> None:
        async with browser_limiter.slot():
            browser_started.set()
            await asyncio.Event().wait()

    pool = _make_pool(max_browsers=1, task_handler=_task_handler)
    job_task = asyncio.create_task(pool._run_job({}, "job-1", "config: yaml"))
    await asyncio.wait_for(browser_started.wait(), timeout=1)

    async def _close() -> None:
        return None

    pool._started = True
    pool._worker = SimpleNamespace(
        allow_pick_jobs=True,
        tasks={"one": job_task},
        main_task=None,
        close=_close,
    )
    pool._runner_task = asyncio.create_task(asyncio.sleep(0))
    monkeypatch.setattr(
        "scrapeyard.queue.pool.get_settings",
        lambda: MagicMock(workers_shutdown_grace_seconds=0.01),
    )

    assert pool.active_browsers == 1
    await pool.stop()

    assert job_task.cancelled()
    assert pool.active_browsers == 0


@pytest.mark.asyncio
async def test_enqueue_raises_memory_error_when_pool_cannot_accept():
    pool = _make_pool(memory_limit_mb=1)
    with patch.object(pool, "_check_memory", return_value=False):
        with pytest.raises(MemoryError, match="memory exceeds"):
            await pool.enqueue("job-1", "config: yaml")


@pytest.mark.asyncio
async def test_enqueue_starts_pool_and_enqueues_job():
    pool = _make_pool()
    fake_redis = MagicMock(
        eval=AsyncMock(return_value=1),
        job_serializer=None,
        expires_extra_ms=86_400_000,
    )

    async def _fake_start() -> None:
        pool._started = True
        pool._redis = fake_redis

    with (
        patch.object(pool, "_check_memory", return_value=True),
        patch.object(pool, "start", _fake_start),
    ):
        result = await pool.enqueue("job-1", "config: yaml", priority="high", run_id="run-1")

    assert hasattr(result, "result")
    fake_redis.eval.assert_awaited_once()
    assert "test-queue:priority:high" in fake_redis.eval.await_args.args
    assert "_defer_until" not in fake_redis.eval.await_args.kwargs


@pytest.mark.asyncio
async def test_duplicate_enqueue_returns_routing_handle_without_second_delivery():
    pool = _make_pool()
    fake_redis = MagicMock(
        eval=AsyncMock(side_effect=[1, 0]),
        job_serializer=None,
        expires_extra_ms=86_400_000,
    )
    pool._started = True
    pool._redis = fake_redis

    with patch.object(pool, "_check_memory", return_value=True):
        first = await pool.enqueue(
            "job-1", "config: yaml", priority="high", run_id="run-1"
        )
        duplicate = await pool.enqueue(
            "job-1", "config: yaml", priority="low", run_id="run-1"
        )

    assert hasattr(first, "result")
    assert hasattr(duplicate, "result")
    assert fake_redis.eval.await_count == 2


@pytest.mark.asyncio
async def test_enqueue_requires_run_id():
    pool = _make_pool()
    pool._started = True
    pool._redis = MagicMock(enqueue_job=AsyncMock(return_value=None))

    with patch.object(pool, "_check_memory", return_value=True):
        with pytest.raises(RuntimeError, match="requires a run_id"):
            await pool.enqueue("job-1", "config: yaml")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pipeline_result", "expected"),
    [
        ([0, 0, 1, None, 99.0, None, None], QueueDeliveryState.queued),
        ([0, 0, 1, None, None, 101.0, None], QueueDeliveryState.deferred),
        ([0, 1, 1, 99.0, None, None, None], QueueDeliveryState.in_progress),
        ([1, 0, 0, None, None, None, None], QueueDeliveryState.complete),
        ([0, 0, 0, None, None, None, None], QueueDeliveryState.missing),
    ],
)
async def test_inspect_delivery_checks_fixed_bounded_queue_set(
    pipeline_result,
    expected,
):
    pool = _make_pool(queue_name="inspection-queue")
    redis = MagicMock()
    pool._redis = redis
    pipeline = MagicMock(execute=AsyncMock(return_value=pipeline_result))
    redis.pipeline.return_value.__aenter__.return_value = pipeline

    with patch("scrapeyard.queue.pool.timestamp_ms", return_value=100):
        result = await pool.inspect_delivery("run-inspect")

    assert result is expected
    assert [call.args[0] for call in pipeline.zscore.call_args_list] == [
        "inspection-queue",
        "inspection-queue:priority:high",
        "inspection-queue:priority:normal",
        "inspection-queue:priority:low",
    ]


@pytest.mark.asyncio
async def test_inspect_delivery_treats_orphaned_queue_member_as_missing():
    pool = _make_pool()
    redis = MagicMock()
    pipeline = MagicMock(
        execute=AsyncMock(
            return_value=[0, 0, 0, None, None, 99.0, None]
        )
    )
    redis.pipeline.return_value.__aenter__.return_value = pipeline
    pool._redis = redis

    result = await pool.inspect_delivery("run-orphaned")

    assert result is QueueDeliveryState.missing


@pytest.mark.asyncio
async def test_inspect_delivery_fails_closed_on_duplicate_queue_membership():
    pool = _make_pool()
    redis = MagicMock()
    pipeline = MagicMock(
        execute=AsyncMock(
            return_value=[0, 0, 1, None, 99.0, None, 99.0]
        )
    )
    redis.pipeline.return_value.__aenter__.return_value = pipeline
    pool._redis = redis

    with pytest.raises(RuntimeError, match="multiple configured queues"):
        await pool.inspect_delivery("run-corrupt")


@pytest.mark.asyncio
async def test_inspect_delivery_requires_connected_redis():
    pool = _make_pool()

    with pytest.raises(RuntimeError, match="active Redis connection"):
        await pool.inspect_delivery("run-missing")


@pytest.mark.asyncio
async def test_cancel_run_reports_unavailable_without_redis():
    result = await _make_pool().cancel_run("run-1")
    assert result.outcome is QueueCancellationOutcome.unavailable
    assert result.quiescent is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "outcome"),
    [
        (QueueDeliveryState.complete, QueueCancellationOutcome.complete),
        (QueueDeliveryState.missing, QueueCancellationOutcome.missing),
    ],
)
async def test_cancel_run_accepts_already_quiescent_delivery(state, outcome):
    pool = _make_pool()
    pool._redis = MagicMock()
    pool._delivery_snapshot = AsyncMock(
        return_value=_DeliverySnapshot(state)
    )

    result = await pool.cancel_run("run-1")

    assert result.outcome is outcome
    assert result.quiescent is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial", "outcome", "source_priority"),
    [
        (
            QueueDeliveryState.queued,
            QueueCancellationOutcome.queued_cancelled,
            "high",
        ),
        (
            QueueDeliveryState.queued,
            QueueCancellationOutcome.queued_cancelled,
            "normal",
        ),
        (
            QueueDeliveryState.deferred,
            QueueCancellationOutcome.deferred_cancelled,
            "low",
        ),
        (
            QueueDeliveryState.in_progress,
            QueueCancellationOutcome.in_progress_cancelled,
            None,
        ),
    ],
)
async def test_cancel_run_aborts_active_delivery_and_verifies_quiescence(
    initial,
    outcome,
    source_priority,
):
    pool = _make_pool(cancellation_grace_seconds=3.5)
    pool._redis = MagicMock()
    queue_name = (
        pool._queue_name
        if initial is QueueDeliveryState.in_progress
        else pool._priority_queues[source_priority]
    )
    pool._delivery_snapshot = AsyncMock(
        side_effect=[
            _DeliverySnapshot(initial, queue_name),
            _DeliverySnapshot(QueueDeliveryState.complete),
        ]
    )
    pool._move_to_execution_queue = AsyncMock(return_value=True)
    arq_job = MagicMock(abort=AsyncMock(return_value=True))

    with patch("scrapeyard.queue.pool.Job", return_value=arq_job):
        result = await pool.cancel_run("run-1")

    assert result.outcome is outcome
    assert result.initial_state is initial
    assert result.final_state is QueueDeliveryState.complete
    arq_job.abort.assert_awaited_once_with(timeout=3.5, poll_delay=0.1)


@pytest.mark.asyncio
async def test_cancel_run_reports_bounded_abort_timeout():
    pool = _make_pool(cancellation_grace_seconds=0.25)
    pool._redis = MagicMock()
    pool._delivery_snapshot = AsyncMock(
        return_value=_DeliverySnapshot(
            QueueDeliveryState.in_progress,
            pool._queue_name,
        )
    )
    arq_job = MagicMock(abort=AsyncMock(side_effect=asyncio.TimeoutError))

    with patch("scrapeyard.queue.pool.Job", return_value=arq_job):
        result = await pool.cancel_run("run-timeout")

    assert result.outcome is QueueCancellationOutcome.timeout
    assert result.quiescent is False


@pytest.mark.asyncio
async def test_cancel_run_reports_redis_failure_without_raising():
    pool = _make_pool()
    pool._redis = MagicMock()
    pool._delivery_snapshot = AsyncMock(side_effect=ConnectionError("redis down"))

    result = await pool.cancel_run("run-unavailable")

    assert result.outcome is QueueCancellationOutcome.unavailable


@pytest.mark.asyncio
async def test_run_job_accepts_legacy_browser_flag_without_holding_a_permit():
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
async def test_browser_bearing_jobs_can_run_concurrently_before_target_fetches():
    pool = _make_pool(max_browsers=1)
    both_started = asyncio.Event()
    release = asyncio.Event()
    started = 0

    async def _execute(*_args, **_kwargs) -> None:
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        await release.wait()

    pool._execute = AsyncMock(side_effect=_execute)
    tasks = [
        asyncio.create_task(
            pool._run_job({}, f"job-{index}", "config: yaml", needs_browser=True)
        )
        for index in range(2)
    ]

    await asyncio.wait_for(both_started.wait(), timeout=1)
    assert pool.active_tasks == 2
    assert pool.active_browsers == 0

    release.set()
    await asyncio.gather(*tasks)
    assert pool.active_tasks == 0


@pytest.mark.asyncio
async def test_active_browsers_tracks_target_limiter_activity():
    pool = _make_pool(max_browsers=2)

    async with pool.browser_limiter.slot():
        assert pool.active_browsers == 1

    assert pool.active_browsers == 0


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
        browser_limiter=pool.browser_limiter,
    )


@pytest.mark.asyncio
async def test_execute_without_task_handler_raises_instead_of_succeeding_silently():
    pool = _make_pool()

    with pytest.raises(RuntimeError, match="requires a task_handler"):
        await pool._execute("job-1", "config: yaml")
