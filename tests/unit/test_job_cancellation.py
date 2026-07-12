"""Atomic jobs.db cancellation state and race coverage for audit item 07."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from scrapeyard.models.job import Job, JobStatus
from scrapeyard.storage.database import get_db, init_db
from scrapeyard.storage.job_store import SQLiteJobStore
from scrapeyard.storage.types import CancellationAction, RunOwnershipError

NOW = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
YAML = "project: test\nname: cancel\ntarget:\n  url: https://example.com\n  selectors:\n    title: h1\n"


@pytest.fixture()
async def store(tmp_path) -> SQLiteJobStore:
    await init_db(str(tmp_path / "db"))
    return SQLiteJobStore()


async def _save_queued(store: SQLiteJobStore, run_id: str = "run-1") -> None:
    await store.save_job(
        Job(
            job_id="job-1",
            project="test",
            name="cancel",
            config_yaml=YAML,
            updated_at=NOW,
            current_run_id=run_id,
            current_trigger="adhoc",
            schedule_cron="*/5 * * * *",
        )
    )


async def _claim(store: SQLiteJobStore, run_id: str = "run-1") -> None:
    await _save_queued(store, run_id)
    assert await store.claim_run(
        run_id,
        "job-1",
        "adhoc",
        hashlib.sha256(YAML.encode()).hexdigest(),
        NOW,
    )


async def test_queued_cancellation_keeps_delivery_identity_without_fake_run(store):
    await _save_queued(store)

    outcome = await store.cancel_job("job-1", NOW + timedelta(seconds=1))

    job = await store.get_job("job-1")
    assert outcome.action is CancellationAction.cancelled
    assert outcome.prior_status is JobStatus.queued
    assert outcome.run_id == "run-1"
    assert outcome.queue_quiescence_required
    assert job.status is JobStatus.cancelled
    assert job.current_run_id == "run-1"
    assert job.schedule_enabled is False
    assert await store.get_job_run("job-1", "run-1") is None


async def test_running_cancellation_atomically_cancels_exact_run_and_parent(store):
    await _claim(store)
    cancelled_at = NOW + timedelta(seconds=2)

    outcome = await store.cancel_job("job-1", cancelled_at)

    job = await store.get_job("job-1")
    run = await store.get_job_run("job-1", "run-1")
    assert outcome.prior_status is JobStatus.running
    assert job.status is JobStatus.cancelled
    assert job.updated_at == cancelled_at
    assert run is not None and run.status is JobStatus.cancelled
    assert run.completed_at == cancelled_at
    async with get_db("jobs.db") as db:
        count = await (await db.execute("SELECT COUNT(*) FROM webhook_deliveries")).fetchone()
    assert count is not None and count[0] == 0


async def test_cancellation_is_idempotent_and_terminal_states_conflict(store):
    await _save_queued(store)
    await store.cancel_job("job-1", NOW)

    repeated = await store.cancel_job("job-1", NOW + timedelta(minutes=1))

    assert repeated.action is CancellationAction.already_cancelled
    assert (await store.get_job("job-1")).updated_at == NOW


@pytest.mark.parametrize(
    "status",
    [JobStatus.complete, JobStatus.partial, JobStatus.failed, JobStatus.deleting],
)
async def test_terminal_and_deleting_cancellation_conflict(store, status):
    await _save_queued(store)
    async with get_db("jobs.db") as db:
        await db.execute("UPDATE jobs SET status = ? WHERE job_id = 'job-1'", (status.value,))
        await db.commit()

    outcome = await store.cancel_job("job-1", NOW)

    assert outcome.action is CancellationAction.conflict
    assert outcome.prior_status is status


async def test_unknown_cancellation_is_typed_missing(store):
    outcome = await store.cancel_job("missing", NOW)
    assert outcome.action is CancellationAction.missing


async def test_claim_vs_cancel_has_exactly_one_state_winner(store):
    await _save_queued(store)

    claimed, cancelled = await asyncio.gather(
        store.claim_run(
            "run-1",
            "job-1",
            "adhoc",
            hashlib.sha256(YAML.encode()).hexdigest(),
            NOW,
        ),
        store.cancel_job("job-1", NOW + timedelta(seconds=1)),
    )

    job = await store.get_job("job-1")
    if claimed:
        assert cancelled.action is CancellationAction.cancelled
        assert cancelled.prior_status is JobStatus.running
        assert job.status is JobStatus.cancelled
        run = await store.get_job_run("job-1", "run-1")
        assert run is not None and run.status is JobStatus.cancelled
    else:
        assert cancelled.prior_status is JobStatus.queued
        assert job.status is JobStatus.cancelled
        assert await store.get_job_run("job-1", "run-1") is None


async def test_finalization_vs_cancel_has_exactly_one_winner(store):
    await _claim(store)
    terminal_at = NOW + timedelta(minutes=1)

    async def _finalize() -> str:
        try:
            await store.finalize_owned_run(
                "job-1",
                "run-1",
                "complete",
                1,
                0,
                terminal_at,
                NOW - timedelta(seconds=1),
            )
        except RunOwnershipError:
            return "lost"
        return "complete"

    finalization, cancellation = await asyncio.gather(
        _finalize(),
        store.cancel_job("job-1", terminal_at),
    )

    job = await store.get_job("job-1")
    assert (finalization, cancellation.action, job.status) in {
        ("complete", CancellationAction.conflict, JobStatus.complete),
        ("lost", CancellationAction.cancelled, JobStatus.cancelled),
    }


async def test_cancelled_run_rejects_heartbeat_and_finalization(store):
    await _claim(store)
    await store.cancel_job("job-1", NOW + timedelta(seconds=1))

    with pytest.raises(RunOwnershipError):
        await store.heartbeat_run("job-1", "run-1", NOW + timedelta(seconds=2))
    with pytest.raises(RunOwnershipError):
        await store.finalize_owned_run(
            "job-1",
            "run-1",
            "complete",
            1,
            0,
            NOW + timedelta(seconds=3),
            NOW - timedelta(seconds=1),
        )
    assert await store.run_is_active("job-1", "run-1") is False


async def test_cancelled_state_cannot_be_requeued_or_reconciled(store):
    await _save_queued(store)
    await store.cancel_job("job-1", NOW)

    assert not await store.queue_run(
        "job-1",
        expected_status=JobStatus.cancelled.value,
        expected_run_id="run-1",
        new_run_id="run-2",
        new_trigger="adhoc",
        queued_at=NOW + timedelta(seconds=1),
    )
    assert await store.list_stale_queued_jobs(NOW + timedelta(days=1)) == []
    assert await store.recover_stale_running_jobs(
        NOW + timedelta(days=1),
        NOW + timedelta(days=1),
    ) == []
