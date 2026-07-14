"""Race-safe reconciliation of stale SQLite queued ownership with arq."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from scrapeyard.models.job import Job, JobStatus
from scrapeyard.queue.pool import QueueDeliveryState
from scrapeyard.queue.reconciliation import (
    QueuedReconciliationError,
    QueuedReconciliationSummary,
    reconcile_stale_queued_jobs,
    start_queued_reconciliation_loop,
)
from scrapeyard.storage.database import get_db, init_db
from scrapeyard.storage.job_store import SQLiteJobStore
from scrapeyard.storage.types import StaleQueuedJob


NOW = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
TIMEOUT = 300
CONFIG_YAML = """
project: recovery
name: queued-recovery
execution:
  priority: high
target:
  url: https://example.com
  fetcher: dynamic
  selectors:
    title: h1
"""


def _stale_job(*, scheduled: bool = False) -> StaleQueuedJob:
    return StaleQueuedJob(
        job_id="job-scheduled" if scheduled else "job-adhoc",
        run_id="run-scheduled" if scheduled else "run-adhoc",
        config_yaml=CONFIG_YAML,
        queued_at=NOW - timedelta(seconds=TIMEOUT + 1),
        trigger="scheduled" if scheduled else "adhoc",
        schedule_cron="*/5 * * * *" if scheduled else None,
        schedule_enabled=not scheduled,
    )


def _mock_pool(state: QueueDeliveryState) -> MagicMock:
    return MagicMock(
        inspect_delivery=AsyncMock(return_value=state),
        enqueue=AsyncMock(),
    )


async def _sqlite_store(tmp_path) -> SQLiteJobStore:
    await init_db(str(tmp_path / "db"))
    return SQLiteJobStore()


async def _save_stale_job(
    store: SQLiteJobStore,
    *,
    job_id: str = "job-adhoc",
    run_id: str = "run-adhoc",
    scheduled: bool = False,
    trigger: str | None = None,
    queued_at: datetime | None = None,
    config_yaml: str = CONFIG_YAML,
) -> None:
    await store.save_job(
        Job(
            job_id=job_id,
            project="recovery",
            name=job_id,
            status=JobStatus.queued,
            config_yaml=config_yaml,
            updated_at=queued_at or NOW - timedelta(seconds=TIMEOUT + 1),
            schedule_cron="*/5 * * * *" if scheduled else None,
            schedule_enabled=not scheduled,
            current_run_id=run_id,
            current_trigger=trigger or ("scheduled" if scheduled else "adhoc"),
        )
    )


async def test_stale_queued_query_ignores_fresh_and_unowned_rows(tmp_path) -> None:
    store = await _sqlite_store(tmp_path)
    await _save_stale_job(store, job_id="stale", run_id="run-stale")
    await _save_stale_job(
        store,
        job_id="fresh",
        run_id="run-fresh",
        queued_at=NOW - timedelta(seconds=TIMEOUT - 1),
    )
    await _save_stale_job(store, job_id="unowned", run_id="run-temporary")
    async with get_db("jobs.db") as db:
        await db.execute(
            "UPDATE jobs SET current_run_id = NULL WHERE job_id = 'unowned'"
        )
        await db.commit()

    rows = await store.list_stale_queued_jobs(
        NOW - timedelta(seconds=TIMEOUT)
    )

    assert [(row.job_id, row.run_id) for row in rows] == [
        ("stale", "run-stale")
    ]


async def test_stale_queued_query_returns_typed_context_in_deterministic_order(
    tmp_path,
) -> None:
    store = await _sqlite_store(tmp_path)
    oldest = NOW - timedelta(minutes=10)
    await _save_stale_job(
        store,
        job_id="job-b",
        run_id="run-b",
        scheduled=True,
        queued_at=oldest,
    )
    await _save_stale_job(
        store,
        job_id="job-a",
        run_id="run-a",
        queued_at=oldest,
    )
    await _save_stale_job(
        store,
        job_id="job-c",
        run_id="run-c",
        queued_at=NOW - timedelta(minutes=6),
    )

    rows = await store.list_stale_queued_jobs(
        NOW - timedelta(seconds=TIMEOUT)
    )

    assert [row.job_id for row in rows] == ["job-a", "job-b", "job-c"]
    assert rows[0].trigger == "adhoc"
    assert rows[1].trigger == "scheduled"
    assert rows[1].config_yaml == CONFIG_YAML
    assert rows[1].schedule_enabled is False

    bounded = await store.list_stale_queued_jobs(
        NOW - timedelta(seconds=TIMEOUT),
        limit=2,
    )
    assert [row.job_id for row in bounded] == ["job-a", "job-b"]


async def test_periodic_reconciliation_uses_bounded_batches(monkeypatch) -> None:
    two_passes = asyncio.Event()
    calls = 0

    async def reconcile_batch(**_kwargs) -> QueuedReconciliationSummary:
        nonlocal calls
        calls += 1
        if calls == 2:
            two_passes.set()
        return QueuedReconciliationSummary(inspected=17)

    reconcile = AsyncMock(side_effect=reconcile_batch)
    monkeypatch.setattr(
        "scrapeyard.queue.reconciliation.reconcile_stale_queued_jobs",
        reconcile,
    )
    store = MagicMock()
    pool = MagicMock()

    task = start_queued_reconciliation_loop(
        job_store=store,
        worker_pool=pool,
        queued_claim_timeout_seconds=300,
        interval_seconds=0.001,
        batch_size=17,
    )
    try:
        await asyncio.wait_for(two_passes.wait(), timeout=1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert reconcile.await_count >= 2
    assert reconcile.await_args_list[0].kwargs == {
        "job_store": store,
        "worker_pool": pool,
        "queued_claim_timeout_seconds": 300,
        "batch_size": 17,
        "batch_offset": 0,
    }
    assert reconcile.await_args_list[1].kwargs["batch_offset"] == 17


@pytest.mark.parametrize(
    "state",
    [
        QueueDeliveryState.queued,
        QueueDeliveryState.deferred,
        QueueDeliveryState.in_progress,
    ],
)
async def test_active_arq_delivery_is_not_duplicated(state) -> None:
    job = _stale_job()
    store = AsyncMock()
    store.list_stale_queued_jobs.return_value = [job]
    pool = _mock_pool(state)

    summary = await reconcile_stale_queued_jobs(
        job_store=store,
        worker_pool=pool,
        queued_claim_timeout_seconds=TIMEOUT,
        now=NOW,
    )

    assert summary.inspected == 1
    assert summary.queue_present == 1
    store.reserve_queued_run_recovery.assert_not_awaited()
    store.fail_queued_run_recovery.assert_not_awaited()
    pool.enqueue.assert_not_awaited()


async def test_missing_adhoc_delivery_reenqueues_original_run_id_idempotently(
    tmp_path,
) -> None:
    store = await _sqlite_store(tmp_path)
    await _save_stale_job(store)
    pool = _mock_pool(QueueDeliveryState.missing)

    first = await reconcile_stale_queued_jobs(
        job_store=store,
        worker_pool=pool,
        queued_claim_timeout_seconds=TIMEOUT,
        now=NOW,
    )
    second = await reconcile_stale_queued_jobs(
        job_store=store,
        worker_pool=pool,
        queued_claim_timeout_seconds=TIMEOUT,
        now=NOW,
    )

    assert first.recovered == 1
    assert second.inspected == 0
    pool.enqueue.assert_awaited_once_with(
        "job-adhoc",
        CONFIG_YAML,
        "high",
        True,
        run_id="run-adhoc",
        trigger="adhoc",
    )
    job = await store.get_job("job-adhoc")
    assert job.status == JobStatus.queued
    assert job.current_run_id == "run-adhoc"
    assert job.updated_at == NOW


async def test_missing_scheduled_delivery_recovers_accepted_run_when_disabled() -> None:
    job = _stale_job(scheduled=True)
    store = AsyncMock()
    store.list_stale_queued_jobs.return_value = [job]
    store.reserve_queued_run_recovery.return_value = True
    pool = _mock_pool(QueueDeliveryState.missing)

    summary = await reconcile_stale_queued_jobs(
        job_store=store,
        worker_pool=pool,
        queued_claim_timeout_seconds=TIMEOUT,
        now=NOW,
    )

    assert summary.recovered == 1
    pool.enqueue.assert_awaited_once_with(
        job.job_id,
        CONFIG_YAML,
        "high",
        True,
        run_id=job.run_id,
        trigger="scheduled",
    )


async def test_missing_manual_delivery_preserves_trigger_for_scheduled_job(
    tmp_path,
) -> None:
    store = await _sqlite_store(tmp_path)
    await _save_stale_job(
        store,
        job_id="job-manual",
        run_id="run-manual",
        scheduled=True,
        trigger="manual",
    )
    pool = _mock_pool(QueueDeliveryState.missing)

    summary = await reconcile_stale_queued_jobs(
        job_store=store,
        worker_pool=pool,
        queued_claim_timeout_seconds=TIMEOUT,
        now=NOW,
    )

    assert summary.recovered == 1
    pool.enqueue.assert_awaited_once_with(
        "job-manual",
        CONFIG_YAML,
        "high",
        True,
        run_id="run-manual",
        trigger="manual",
    )
    assert await store.claim_run(
        "run-manual",
        "job-manual",
        "manual",
        hashlib.sha256(CONFIG_YAML.encode()).hexdigest(),
        NOW,
    )
    run = await store.get_job_run("job-manual", "run-manual")
    assert run is not None
    assert run.trigger == "manual"


async def test_redis_inspection_failure_aborts_reconciliation() -> None:
    job = _stale_job()
    store = AsyncMock()
    store.list_stale_queued_jobs.return_value = [job]
    pool = MagicMock(
        inspect_delivery=AsyncMock(side_effect=ConnectionError("redis unavailable")),
        enqueue=AsyncMock(),
    )

    with pytest.raises(QueuedReconciliationError, match="startup aborted"):
        await reconcile_stale_queued_jobs(
            job_store=store,
            worker_pool=pool,
            queued_claim_timeout_seconds=TIMEOUT,
            now=NOW,
        )

    store.reserve_queued_run_recovery.assert_not_awaited()
    store.fail_queued_run_recovery.assert_not_awaited()
    pool.enqueue.assert_not_awaited()


async def test_enqueue_failure_conditionally_leaves_terminal_state(tmp_path) -> None:
    store = await _sqlite_store(tmp_path)
    await _save_stale_job(store)
    pool = _mock_pool(QueueDeliveryState.missing)
    pool.enqueue.side_effect = ConnectionError("redis write failed")

    summary = await reconcile_stale_queued_jobs(
        job_store=store,
        worker_pool=pool,
        queued_claim_timeout_seconds=TIMEOUT,
        now=NOW,
    )

    assert summary.failed == 1
    job = await store.get_job("job-adhoc")
    assert job.status == JobStatus.failed
    assert job.current_run_id == "run-adhoc"


async def test_completed_arq_record_is_conditionally_failed_not_reenqueued(
    tmp_path,
) -> None:
    store = await _sqlite_store(tmp_path)
    await _save_stale_job(store)
    pool = _mock_pool(QueueDeliveryState.complete)

    summary = await reconcile_stale_queued_jobs(
        job_store=store,
        worker_pool=pool,
        queued_claim_timeout_seconds=TIMEOUT,
        now=NOW,
    )

    assert summary.failed == 1
    assert (await store.get_job("job-adhoc")).status == JobStatus.failed
    pool.enqueue.assert_not_awaited()


async def test_unreconstructable_stored_config_is_conditionally_failed(
    tmp_path,
) -> None:
    store = await _sqlite_store(tmp_path)
    await _save_stale_job(store, config_yaml="not: valid: yaml")
    pool = _mock_pool(QueueDeliveryState.missing)

    summary = await reconcile_stale_queued_jobs(
        job_store=store,
        worker_pool=pool,
        queued_claim_timeout_seconds=TIMEOUT,
        now=NOW,
    )

    assert summary.failed == 1
    assert (await store.get_job("job-adhoc")).status == JobStatus.failed
    pool.enqueue.assert_not_awaited()


async def test_worker_claim_racing_reconciliation_makes_recovery_a_noop(
    tmp_path,
) -> None:
    store = await _sqlite_store(tmp_path)
    await _save_stale_job(store)
    pool = _mock_pool(QueueDeliveryState.missing)

    async def _claim_before_inspection_returns(_run_id: str) -> QueueDeliveryState:
        claimed = await store.claim_run(
            "run-adhoc",
            "job-adhoc",
            "adhoc",
            "hash",
            NOW,
        )
        assert claimed is True
        return QueueDeliveryState.missing

    pool.inspect_delivery.side_effect = _claim_before_inspection_returns

    summary = await reconcile_stale_queued_jobs(
        job_store=store,
        worker_pool=pool,
        queued_claim_timeout_seconds=TIMEOUT,
        now=NOW,
    )

    assert summary.race_noop == 1
    assert (await store.get_job("job-adhoc")).status == JobStatus.running
    pool.enqueue.assert_not_awaited()


async def test_current_run_replacement_racing_reconciliation_is_a_noop(
    tmp_path,
) -> None:
    store = await _sqlite_store(tmp_path)
    await _save_stale_job(store)
    pool = _mock_pool(QueueDeliveryState.missing)

    async def _replace_before_inspection_returns(_run_id: str) -> QueueDeliveryState:
        replaced = await store.queue_run(
            "job-adhoc",
            expected_status="queued",
            expected_run_id="run-adhoc",
            new_run_id="run-new",
            new_trigger="adhoc",
            queued_at=NOW,
        )
        assert replaced is True
        return QueueDeliveryState.missing

    pool.inspect_delivery.side_effect = _replace_before_inspection_returns

    summary = await reconcile_stale_queued_jobs(
        job_store=store,
        worker_pool=pool,
        queued_claim_timeout_seconds=TIMEOUT,
        now=NOW,
    )

    assert summary.race_noop == 1
    job = await store.get_job("job-adhoc")
    assert job.status == JobStatus.queued
    assert job.current_run_id == "run-new"
    pool.enqueue.assert_not_awaited()


async def test_stale_failure_snapshot_cannot_fail_newer_same_run_reservation(
    tmp_path,
) -> None:
    store = await _sqlite_store(tmp_path)
    await _save_stale_job(store)
    old_queued_at = NOW - timedelta(seconds=TIMEOUT + 1)
    reserved = await store.reserve_queued_run_recovery(
        "job-adhoc",
        "run-adhoc",
        expected_queued_at=old_queued_at,
        stale_before=NOW - timedelta(seconds=TIMEOUT),
        reserved_at=NOW,
    )

    failed = await store.fail_queued_run_recovery(
        "job-adhoc",
        "run-adhoc",
        expected_queued_at=old_queued_at,
        failed_at=NOW,
    )

    assert reserved is True
    assert failed is False
    job = await store.get_job("job-adhoc")
    assert job.status == JobStatus.queued
    assert job.current_run_id == "run-adhoc"
    assert job.updated_at == NOW
