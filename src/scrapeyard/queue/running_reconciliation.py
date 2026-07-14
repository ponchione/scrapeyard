"""Periodic repair for stale running ownership and terminal webhook intent."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from scrapeyard.common.time import utc_now
from scrapeyard.queue.terminal_reconciliation import (
    TerminalIntentReconciliationSummary,
    reconcile_terminal_webhook_intents,
)
from scrapeyard.runtime.background import BackgroundLoopMonitor
from scrapeyard.storage.protocols import JobStore, ResultStore
from scrapeyard.webhook.dispatcher import WebhookNotifier

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RunningReconciliationSummary:
    recovered: int
    terminal_intents: TerminalIntentReconciliationSummary


async def reconcile_stale_running_jobs(
    *,
    job_store: JobStore,
    result_store: ResultStore,
    heartbeat_timeout_seconds: int,
    batch_size: int,
    webhook_notifier: WebhookNotifier | None = None,
    now: datetime | None = None,
) -> RunningReconciliationSummary:
    """Recover one bounded stale-running batch and repair terminal intent."""

    recovered_at = now or utc_now()
    cutoff = recovered_at - timedelta(seconds=heartbeat_timeout_seconds)
    recoveries = await job_store.recover_stale_running_jobs(
        cutoff,
        recovered_at,
        limit=batch_size,
    )
    for recovery in recoveries:
        logger.warning(
            "Recovered running state job_id=%s run_id=%s last_heartbeat=%s "
            "timeout_seconds=%s recovery_action=%s",
            recovery.job_id,
            recovery.run_id,
            (
                recovery.last_heartbeat_at.isoformat()
                if recovery.last_heartbeat_at is not None
                else None
            ),
            heartbeat_timeout_seconds,
            recovery.action,
        )

    terminal = await reconcile_terminal_webhook_intents(
        job_store=job_store,
        result_store=result_store,
        batch_size=batch_size,
    )
    if terminal.repaired and webhook_notifier is not None:
        await webhook_notifier.notify()
    logger.info(
        "Running reconciliation finished recovered_count=%s "
        "terminal_inspected_count=%s terminal_repaired_count=%s",
        len(recoveries),
        terminal.inspected,
        terminal.repaired,
    )
    return RunningReconciliationSummary(
        recovered=len(recoveries),
        terminal_intents=terminal,
    )


def start_running_reconciliation_loop(
    *,
    job_store: JobStore,
    result_store: ResultStore,
    heartbeat_timeout_seconds: int,
    interval_seconds: float,
    batch_size: int,
    webhook_notifier: WebhookNotifier | None = None,
    monitor: BackgroundLoopMonitor | None = None,
) -> asyncio.Task[None]:
    """Start bounded stale-running recovery under interval-aware supervision."""

    loop_monitor = monitor or BackgroundLoopMonitor(
        "running_reconciliation",
        interval_seconds=interval_seconds,
    )

    async def _loop() -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                await reconcile_stale_running_jobs(
                    job_store=job_store,
                    result_store=result_store,
                    heartbeat_timeout_seconds=heartbeat_timeout_seconds,
                    batch_size=batch_size,
                    webhook_notifier=webhook_notifier,
                )
                loop_monitor.record_success()
            except Exception as exc:
                loop_monitor.record_failure(exc)
                logger.exception("Periodic running reconciliation failed; retrying next interval")

    task = asyncio.create_task(
        _loop(),
        name="scrapeyard-running-reconciliation",
    )
    loop_monitor.bind(task)
    return task
