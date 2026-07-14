"""Supervision contracts for queued and running durability repair loops."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from scrapeyard.queue.reconciliation import (
    QueuedReconciliationSummary,
    start_queued_reconciliation_loop,
)
from scrapeyard.queue.running_reconciliation import (
    reconcile_stale_running_jobs,
    start_running_reconciliation_loop,
)
from scrapeyard.queue.terminal_reconciliation import (
    TerminalIntentReconciliationSummary,
)
from scrapeyard.runtime.background import BackgroundLoopMonitor
from scrapeyard.runtime.health import probe_background_service
from scrapeyard.runtime.metrics import RECONCILIATION_PASSES
from scrapeyard.storage.types import RunRecovery


def _pass_count(service: str, status: str) -> float:
    return sum(
        sample.value
        for family in RECONCILIATION_PASSES.collect()
        for sample in family.samples
        if sample.name == "scrapeyard_reconciliation_passes_total"
        and sample.labels == {"service": service, "status": status}
    )


async def test_queued_loop_reports_repeated_failures_then_recovers(
    monkeypatch,
) -> None:
    clock = [0.0]
    monitor = BackgroundLoopMonitor(
        "queued_reconciliation",
        interval_seconds=1,
        clock=lambda: clock[0],
    )
    three_failures = asyncio.Event()
    allow_success = asyncio.Event()
    success_returned = asyncio.Event()
    calls = 0

    async def reconcile(**_kwargs):
        nonlocal calls
        calls += 1
        clock[0] += 1
        if calls <= 3:
            if calls == 3:
                three_failures.set()
            raise RuntimeError("repair unavailable")
        await allow_success.wait()
        success_returned.set()
        return QueuedReconciliationSummary()

    monkeypatch.setattr(
        "scrapeyard.queue.reconciliation.reconcile_stale_queued_jobs",
        reconcile,
    )
    failures_before = _pass_count("queued_reconciliation", "failure")
    task = start_queued_reconciliation_loop(
        job_store=MagicMock(),
        worker_pool=MagicMock(),
        queued_claim_timeout_seconds=300,
        interval_seconds=0.001,
        batch_size=10,
        monitor=monitor,
    )
    try:
        await asyncio.wait_for(three_failures.wait(), timeout=1)
        await asyncio.sleep(0.01)
        assert not task.done()
        assert monitor.consecutive_failures == 3
        assert monitor.last_failure_at == 3
        assert probe_background_service("queued_reconciliation", monitor).ok is False
        assert _pass_count("queued_reconciliation", "failure") == failures_before + 3

        allow_success.set()
        await asyncio.wait_for(success_returned.wait(), timeout=1)
        for _ in range(20):
            if monitor.consecutive_failures == 0:
                break
            await asyncio.sleep(0)
        assert probe_background_service("queued_reconciliation", monitor).ok is True
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_running_reconciliation_is_bounded_and_repairs_terminal_intent(
    monkeypatch,
) -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    recovery = RunRecovery(
        job_id="job-1",
        run_id="run-1",
        action="failed_stale_heartbeat",
        last_heartbeat_at=now - timedelta(seconds=601),
    )
    job_store = AsyncMock()
    job_store.recover_stale_running_jobs.return_value = [recovery]
    terminal = TerminalIntentReconciliationSummary(inspected=1, repaired=1)
    repair_terminal = AsyncMock(return_value=terminal)
    monkeypatch.setattr(
        "scrapeyard.queue.running_reconciliation.reconcile_terminal_webhook_intents",
        repair_terminal,
    )
    notifier = AsyncMock()

    summary = await reconcile_stale_running_jobs(
        job_store=job_store,
        result_store="results",
        heartbeat_timeout_seconds=600,
        batch_size=17,
        webhook_notifier=notifier,
        now=now,
    )

    assert summary.recovered == 1
    job_store.recover_stale_running_jobs.assert_awaited_once_with(
        now - timedelta(seconds=600),
        now,
        limit=17,
    )
    repair_terminal.assert_awaited_once_with(
        job_store=job_store,
        result_store="results",
        batch_size=17,
    )
    notifier.notify.assert_awaited_once_with()


async def test_running_loop_failure_changes_health_and_failure_metric(
    monkeypatch,
) -> None:
    clock = [0.0]
    monitor = BackgroundLoopMonitor(
        "running_reconciliation",
        interval_seconds=1,
        clock=lambda: clock[0],
    )
    failed = asyncio.Event()
    calls = 0

    async def reconcile(**_kwargs):
        nonlocal calls
        calls += 1
        clock[0] += 1
        if calls == 3:
            failed.set()
        raise RuntimeError("jobs database unavailable")

    monkeypatch.setattr(
        "scrapeyard.queue.running_reconciliation.reconcile_stale_running_jobs",
        reconcile,
    )
    failures_before = _pass_count("running_reconciliation", "failure")
    task = start_running_reconciliation_loop(
        job_store=MagicMock(),
        result_store=MagicMock(),
        heartbeat_timeout_seconds=600,
        interval_seconds=0.001,
        batch_size=10,
        monitor=monitor,
    )
    try:
        await asyncio.wait_for(failed.wait(), timeout=1)
        await asyncio.sleep(0)
        probe = probe_background_service("running_reconciliation", monitor)
        assert probe.ok is False
        assert "consecutive_failures=3" in (probe.detail or "")
        assert _pass_count("running_reconciliation", "failure") == failures_before + 3
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
