"""Durable job/run/error/tombstone history retention tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from scrapeyard.models.job import (
    ActionTaken,
    ErrorRecord,
    ErrorType,
    Job,
    JobStatus,
)
from scrapeyard.storage.database import get_db, init_db
from scrapeyard.storage.error_store import SQLiteErrorStore
from scrapeyard.storage.job_store import SQLiteJobStore
from scrapeyard.storage.cleanup import (
    CleanupIncompleteError,
    HistoryRetentionPolicy,
    run_cleanup,
)
from scrapeyard.storage.types import ResultReconciliationReport


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=90)


@pytest.fixture
async def stores(tmp_path):
    await init_db(str(tmp_path / "db"))
    return SQLiteJobStore(), SQLiteErrorStore()


async def _save_job(
    store: SQLiteJobStore,
    job_id: str,
    *,
    status: JobStatus = JobStatus.complete,
    updated_at: datetime = OLD,
    schedule: bool = False,
    current_run_id: str | None = None,
    deleting_policy: bool | None = None,
) -> None:
    await store.save_job(
        Job(
            job_id=job_id,
            project="history",
            name=job_id,
            status=status,
            config_yaml="project: history\nname: retained",
            created_at=OLD,
            updated_at=updated_at,
            schedule_cron="0 * * * *" if schedule else None,
            current_run_id=current_run_id,
            deletion_requested_at=OLD if status is JobStatus.deleting else None,
            delete_results_on_delete=deleting_policy,
        )
    )


async def _insert_run(
    job_id: str,
    run_id: str,
    *,
    started_at: datetime,
    status: str = "complete",
    reconciled: bool = True,
) -> None:
    timestamp = started_at.isoformat()
    async with get_db("jobs.db") as db:
        await db.execute(
            """INSERT INTO job_runs
               (run_id, job_id, status, trigger, config_hash, started_at,
                heartbeat_at, completed_at, record_count, error_count,
                webhook_reconciled_at)
               VALUES (?, ?, ?, 'scheduled', 'hash', ?, ?, ?, 1, 0, ?)""",
            (
                run_id,
                job_id,
                status,
                timestamp,
                timestamp,
                timestamp,
                timestamp if reconciled else None,
            ),
        )
        await db.commit()


async def _insert_delivery(
    job_id: str,
    run_id: str,
    *,
    status: str,
    scrubbed_at: datetime | None,
) -> None:
    timestamp = OLD.isoformat()
    terminal_at = timestamp if status == "delivered" else None
    failed_at = timestamp if status == "failed" else None
    async with get_db("jobs.db") as db:
        await db.execute(
            """INSERT INTO webhook_deliveries
               (delivery_id, job_id, run_id, event, url, headers_json,
                payload_json, status, next_attempt_at, delivered_at, failed_at,
                scrubbed_at, created_at, updated_at)
               VALUES (?, ?, ?, 'job.complete', 'scrubbed:', '{}', '{}', ?,
                       ?, ?, ?, ?, ?, ?)""",
            (
                f"delivery-{job_id}-{run_id}",
                job_id,
                run_id,
                status,
                timestamp,
                terminal_at,
                failed_at,
                None if scrubbed_at is None else scrubbed_at.isoformat(),
                timestamp,
                timestamp,
            ),
        )
        await db.commit()


async def test_adhoc_candidates_exclude_active_fresh_and_unsafe_webhooks(stores):
    jobs, _errors = stores
    await _save_job(jobs, "eligible")
    await _save_job(jobs, "active", status=JobStatus.running)
    await _save_job(jobs, "fresh", updated_at=NOW)
    await _save_job(jobs, "pending")
    await _insert_delivery("pending", "run", status="pending", scrubbed_at=None)
    await _save_job(jobs, "recent-tombstone")
    await _insert_delivery(
        "recent-tombstone",
        "run",
        status="delivered",
        scrubbed_at=NOW - timedelta(days=5),
    )
    await _save_job(jobs, "old-tombstone")
    await _insert_delivery(
        "old-tombstone",
        "run",
        status="delivered",
        scrubbed_at=OLD,
    )
    await _save_job(
        jobs,
        "resumable",
        status=JobStatus.deleting,
        deleting_policy=False,
    )

    candidates = await jobs.list_adhoc_jobs_for_retention(
        NOW - timedelta(days=30),
        tombstone_expired_before=NOW - timedelta(days=30),
        limit=20,
    )

    assert set(candidates) == {"eligible", "old-tombstone", "resumable"}


async def test_adhoc_candidate_selection_is_bounded(stores):
    jobs, _errors = stores
    for index in range(8):
        await _save_job(jobs, f"job-{index}")

    candidates = await jobs.list_adhoc_jobs_for_retention(
        NOW - timedelta(days=30),
        tombstone_expired_before=NOW - timedelta(days=30),
        limit=3,
    )

    assert candidates == ["job-0", "job-1", "job-2"]


async def test_scheduled_pruning_is_atomic_and_preserves_lifetime_stats(stores):
    jobs, _errors = stores
    await _save_job(
        jobs,
        "schedule",
        schedule=True,
        current_run_id="current",
    )
    await _insert_run("schedule", "old", started_at=OLD)
    await _insert_run("schedule", "tombstone", started_at=OLD + timedelta(hours=1))
    await _insert_delivery(
        "schedule",
        "tombstone",
        status="delivered",
        scrubbed_at=OLD,
    )
    await _insert_run("schedule", "pending", started_at=OLD + timedelta(hours=2))
    await _insert_delivery("schedule", "pending", status="pending", scrubbed_at=None)
    await _insert_run(
        "schedule",
        "unreconciled",
        started_at=OLD + timedelta(hours=3),
        reconciled=False,
    )
    await _insert_run("schedule", "recent", started_at=NOW - timedelta(days=1))
    await _insert_run("schedule", "current", started_at=OLD + timedelta(hours=4))

    candidates = await jobs.list_scheduled_runs_for_retention(
        NOW - timedelta(days=30),
        tombstone_expired_before=NOW - timedelta(days=30),
        max_runs_per_job=100,
        limit=20,
    )

    assert candidates == [("schedule", "old"), ("schedule", "tombstone")]
    first = await jobs.prune_scheduled_run_for_retention(
        "schedule",
        "old",
        expired_before=NOW - timedelta(days=30),
        tombstone_expired_before=NOW - timedelta(days=30),
        max_runs_per_job=100,
    )
    second = await jobs.prune_scheduled_run_for_retention(
        "schedule",
        "tombstone",
        expired_before=NOW - timedelta(days=30),
        tombstone_expired_before=NOW - timedelta(days=30),
        max_runs_per_job=100,
    )

    assert first.pruned and first.webhook_tombstones_deleted == 0
    assert second.pruned and second.webhook_tombstones_deleted == 1
    assert await jobs.get_job_run("schedule", "old") is None
    assert await jobs.get_job_run("schedule", "tombstone") is None
    assert await jobs.get_job_run_stats("schedule") == (6, NOW - timedelta(days=1))
    reconciliation_candidates = await jobs.list_terminal_webhook_candidates()
    assert {candidate.run_id for candidate in reconciliation_candidates}.isdisjoint(
        {"old", "tombstone"}
    )
    async with get_db("jobs.db") as db:
        remaining = await (
            await db.execute(
                "SELECT COUNT(*) FROM webhook_deliveries WHERE run_id = 'tombstone'"
            )
        ).fetchone()
    assert remaining[0] == 0


async def test_scheduled_count_retention_prunes_beyond_lifetime_limit(stores):
    jobs, _errors = stores
    await _save_job(
        jobs,
        "counted",
        schedule=True,
        current_run_id="run-3",
    )
    for index in range(4):
        await _insert_run(
            "counted",
            f"run-{index}",
            started_at=NOW - timedelta(hours=4 - index),
        )

    candidates = await jobs.list_scheduled_runs_for_retention(
        NOW - timedelta(days=30),
        tombstone_expired_before=NOW - timedelta(days=30),
        max_runs_per_job=2,
        limit=20,
    )

    assert candidates == [("counted", "run-0"), ("counted", "run-1")]


async def test_error_cleanup_batches_are_deterministic_and_resumable(stores):
    _jobs, errors = stores
    for index in range(5):
        await errors.log_error(
            ErrorRecord(
                job_id="job",
                run_id="run",
                project="history",
                target_url="https://example.com",
                attempt=1,
                timestamp=OLD + timedelta(seconds=index),
                error_type=ErrorType.network_error,
                fetcher_used="basic",
                action_taken=ActionTaken.fail,
            )
        )

    first = await errors.delete_errors_for_job_batch("job", limit=2)
    second = await errors.delete_errors_for_run_batch("run", limit=2)
    final = await errors.delete_expired_errors(NOW, limit=1)

    assert first == (2, True)
    assert second == (2, True)
    assert final == 1
    assert await errors.count_errors_for_run("run") == 0


async def test_real_adhoc_cleanup_resumes_after_cross_database_failure(stores):
    jobs, _errors = stores
    await _save_job(jobs, "crash-safe")
    backing_errors = SQLiteErrorStore()
    await backing_errors.log_error(
        ErrorRecord(
            job_id="crash-safe",
            run_id="run",
            project="history",
            target_url="https://example.com",
            attempt=1,
            timestamp=NOW,
            error_type=ErrorType.network_error,
            fetcher_used="basic",
            action_taken=ActionTaken.fail,
        )
    )

    class FailOnceErrorStore(SQLiteErrorStore):
        failed = False

        async def delete_errors_for_job_batch(self, job_id, *, limit):
            if not self.failed:
                self.failed = True
                raise RuntimeError("injected errors.db failure")
            return await super().delete_errors_for_job_batch(job_id, limit=limit)

    errors = FailOnceErrorStore()
    results = AsyncMock()
    results.delete_expired.return_value = 0
    results.prune_excess_per_job.return_value = 0
    results.reconcile_artifacts.return_value = ResultReconciliationReport(dry_run=True)
    policy = HistoryRetentionPolicy(
        adhoc_job_retention_days=30,
        scheduled_run_retention_days=30,
        scheduled_run_retention_count=100,
        error_retention_days=365,
        webhook_tombstone_retention_days=30,
        adhoc_job_batch_size=10,
        scheduled_run_batch_size=10,
        error_batch_size=10,
    )

    with pytest.raises(CleanupIncompleteError, match="adhoc_errors"):
        await run_cleanup(
            results,
            retention_days=30,
            max_results_per_job=100,
            job_store=jobs,
            error_store=errors,
            history_policy=policy,
            now=NOW,
        )

    assert (await jobs.get_job("crash-safe")).status is JobStatus.deleting

    await run_cleanup(
        results,
        retention_days=30,
        max_results_per_job=100,
        job_store=jobs,
        error_store=errors,
        history_policy=policy,
        now=NOW,
    )

    with pytest.raises(KeyError):
        await jobs.get_job("crash-safe")
    assert await backing_errors.count_errors_for_run("run") == 0
    results.delete_results.assert_not_awaited()
