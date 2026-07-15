"""Test the result retention cleanup loop."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scrapeyard.storage.cleanup import CleanupCycleOutcome, start_cleanup_loop
from scrapeyard.runtime.background import BackgroundLoopMonitor
from scrapeyard.runtime.health import probe_background_service


@pytest.mark.asyncio
async def test_cleanup_loop_runs_periodically():
    mock_run_cleanup = AsyncMock()
    mock_result_store = MagicMock()

    with patch("scrapeyard.storage.cleanup.run_cleanup", mock_run_cleanup):
        task = start_cleanup_loop(mock_result_store, interval_hours=0.05 / 3600)
        await asyncio.sleep(0.15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert mock_run_cleanup.call_count >= 2
    assert all(
        call.kwargs["result_store"] is mock_result_store
        for call in mock_run_cleanup.await_args_list
    )


@pytest.mark.asyncio
async def test_cleanup_loop_handles_cancellation():
    mock_run_cleanup = AsyncMock()
    mock_result_store = MagicMock()

    with patch("scrapeyard.storage.cleanup.run_cleanup", mock_run_cleanup):
        task = start_cleanup_loop(mock_result_store, interval_hours=0.05 / 3600)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_cleanup_loop_promptly_reschedules_saturated_cycle(monkeypatch):
    delays: list[float] = []
    settings = SimpleNamespace(
        storage_retention_days=7,
        storage_max_results_per_job=20,
        storage_cleanup_batch_size=50,
        storage_orphan_grace_seconds=3600,
        storage_reconciliation_dry_run=True,
        storage_cleanup_cycle_max_items_per_phase=100,
        storage_cleanup_cycle_max_seconds=30,
        storage_cleanup_catchup_delay_seconds=2.5,
    )

    async def fake_run_cleanup(**_kwargs):
        return CleanupCycleOutcome(saturated=True, processed_items=100)

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        raise asyncio.CancelledError

    monkeypatch.setattr("scrapeyard.storage.cleanup.get_settings", lambda: settings)
    monkeypatch.setattr("scrapeyard.storage.cleanup.run_cleanup", fake_run_cleanup)
    monkeypatch.setattr("scrapeyard.storage.cleanup.asyncio.sleep", fake_sleep)

    task = start_cleanup_loop(MagicMock(), interval_hours=6)
    with pytest.raises(asyncio.CancelledError):
        await task

    assert delays == [2.5]


@pytest.mark.asyncio
async def test_cleanup_loop_reads_settings_each_iteration(monkeypatch):
    calls = []
    settings = iter(
        [
            SimpleNamespace(
                storage_retention_days=7,
                storage_max_results_per_job=20,
                storage_cleanup_batch_size=50,
                storage_orphan_grace_seconds=3600,
                storage_reconciliation_dry_run=True,
            ),
            SimpleNamespace(
                storage_retention_days=14,
                storage_max_results_per_job=40,
                storage_cleanup_batch_size=75,
                storage_orphan_grace_seconds=7200,
                storage_reconciliation_dry_run=False,
            ),
        ]
    )

    async def fake_run_cleanup(**kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr("scrapeyard.storage.cleanup.get_settings", lambda: next(settings))
    monkeypatch.setattr("scrapeyard.storage.cleanup.run_cleanup", fake_run_cleanup)

    task = start_cleanup_loop(MagicMock(), interval_hours=0)
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [call["retention_days"] for call in calls] == [7, 14]
    assert [call["max_results_per_job"] for call in calls] == [20, 40]
    assert [call["result_cleanup_batch_size"] for call in calls] == [50, 75]
    assert [call["orphan_grace_seconds"] for call in calls] == [3600, 7200]
    assert [call["reconciliation_dry_run"] for call in calls] == [True, False]


@pytest.mark.asyncio
async def test_cleanup_loop_wires_webhook_retention_settings(monkeypatch):
    calls = []
    settings = SimpleNamespace(
        storage_retention_days=7,
        storage_max_results_per_job=20,
        storage_cleanup_batch_size=50,
        webhook_delivered_retention_days=2,
        webhook_failed_retention_days=14,
        webhook_dispatch_batch_size=25,
        storage_orphan_grace_seconds=86400,
        storage_reconciliation_dry_run=True,
    )

    async def fake_run_cleanup(**kwargs):
        calls.append(kwargs)
        raise asyncio.CancelledError

    monkeypatch.setattr("scrapeyard.storage.cleanup.get_settings", lambda: settings)
    monkeypatch.setattr("scrapeyard.storage.cleanup.run_cleanup", fake_run_cleanup)
    outbox = MagicMock()

    task = start_cleanup_loop(MagicMock(), outbox, interval_hours=0)
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls[0]["webhook_outbox_store"] is outbox
    assert calls[0]["webhook_delivered_retention_days"] == 2
    assert calls[0]["webhook_failed_retention_days"] == 14
    assert calls[0]["webhook_cleanup_batch_size"] == 25
    assert calls[0]["result_cleanup_batch_size"] == 50
    assert calls[0]["orphan_grace_seconds"] == 86400
    assert calls[0]["reconciliation_dry_run"] is True


@pytest.mark.asyncio
async def test_cleanup_loop_wires_durable_history_policy(monkeypatch):
    calls = []
    settings = SimpleNamespace(
        storage_retention_days=7,
        storage_max_results_per_job=20,
        storage_cleanup_batch_size=50,
        storage_orphan_grace_seconds=86400,
        storage_reconciliation_dry_run=True,
        idempotency_cleanup_batch_size=30,
        history_adhoc_job_retention_days=31,
        history_scheduled_run_retention_days=32,
        history_scheduled_run_retention_count=33,
        history_error_retention_days=34,
        history_webhook_tombstone_retention_days=35,
        history_adhoc_job_cleanup_batch_size=36,
        history_scheduled_run_cleanup_batch_size=37,
        history_error_cleanup_batch_size=38,
    )

    async def fake_run_cleanup(**kwargs):
        calls.append(kwargs)
        raise asyncio.CancelledError

    monkeypatch.setattr("scrapeyard.storage.cleanup.get_settings", lambda: settings)
    monkeypatch.setattr("scrapeyard.storage.cleanup.run_cleanup", fake_run_cleanup)
    job_store = MagicMock()
    error_store = MagicMock()

    task = start_cleanup_loop(
        MagicMock(),
        interval_hours=0,
        job_store=job_store,
        error_store=error_store,
    )
    with pytest.raises(asyncio.CancelledError):
        await task

    policy = calls[0]["history_policy"]
    assert calls[0]["job_store"] is job_store
    assert calls[0]["error_store"] is error_store
    assert policy.adhoc_job_retention_days == 31
    assert policy.scheduled_run_retention_days == 32
    assert policy.scheduled_run_retention_count == 33
    assert policy.error_retention_days == 34
    assert policy.webhook_tombstone_retention_days == 35
    assert policy.adhoc_job_batch_size == 36
    assert policy.scheduled_run_batch_size == 37
    assert policy.error_batch_size == 38


@pytest.mark.asyncio
async def test_cleanup_monitor_allows_one_failure_then_records_recovery(
    monkeypatch,
) -> None:
    clock = [0.0]
    monitor = BackgroundLoopMonitor(
        "cleanup",
        interval_seconds=1,
        clock=lambda: clock[0],
    )
    recovered = asyncio.Event()
    calls = 0

    async def fake_run_cleanup(**_kwargs):
        nonlocal calls
        calls += 1
        clock[0] += 1
        if calls == 1:
            raise RuntimeError("transient")
        if calls == 2:
            recovered.set()
            return
        raise asyncio.CancelledError

    monkeypatch.setattr("scrapeyard.storage.cleanup.run_cleanup", fake_run_cleanup)
    task = start_cleanup_loop(
        MagicMock(),
        interval_hours=0,
        monitor=monitor,
    )
    with pytest.raises(asyncio.CancelledError):
        await task

    assert recovered.is_set()
    assert monitor.consecutive_failures == 0


@pytest.mark.asyncio
async def test_cleanup_monitor_exposes_repeated_incomplete_passes(
    monkeypatch,
) -> None:
    clock = [0.0]
    monitor = BackgroundLoopMonitor(
        "cleanup",
        interval_seconds=1,
        clock=lambda: clock[0],
    )
    failed_twice = asyncio.Event()
    calls = 0

    async def fake_run_cleanup(**_kwargs):
        nonlocal calls
        calls += 1
        clock[0] += 1
        if calls == 2:
            failed_twice.set()
        raise RuntimeError("cleanup phases failed")

    monkeypatch.setattr("scrapeyard.storage.cleanup.run_cleanup", fake_run_cleanup)
    task = start_cleanup_loop(
        MagicMock(),
        interval_hours=0,
        monitor=monitor,
    )
    try:
        await asyncio.wait_for(failed_twice.wait(), timeout=1)
        await asyncio.sleep(0)
        probe = probe_background_service("cleanup", monitor)
        assert probe.ok is False
        assert "consecutive_failures=" in (probe.detail or "")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cleanup_monitor_reports_stopped_task(monkeypatch) -> None:
    monitor = BackgroundLoopMonitor("cleanup", interval_seconds=1)

    async def stop_after_pass(**_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr("scrapeyard.storage.cleanup.run_cleanup", stop_after_pass)
    task = start_cleanup_loop(MagicMock(), interval_hours=0, monitor=monitor)
    await asyncio.gather(task, return_exceptions=True)

    probe = probe_background_service("cleanup", monitor)
    assert probe.ok is False
    assert probe.detail == "cleanup task stopped"
