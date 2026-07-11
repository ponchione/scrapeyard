"""Real-SQLite tests for run ownership compare-and-set operations."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest

from scrapeyard.models.job import Job, JobStatus
from scrapeyard.storage.database import get_db, init_db
from scrapeyard.storage.job_store import SQLiteJobStore
from scrapeyard.storage.types import RunOwnershipError


NOW = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
async def store(tmp_path) -> SQLiteJobStore:
    await init_db(str(tmp_path / "db"))
    return SQLiteJobStore()


async def _save_queued(
    store: SQLiteJobStore,
    *,
    job_id: str = "job-1",
    run_id: str = "run-1",
) -> None:
    await store.save_job(
        Job(
            job_id=job_id,
            project="test",
            name=job_id,
            config_yaml="project: test",
            status=JobStatus.queued,
            updated_at=NOW,
            current_run_id=run_id,
        )
    )


async def _claim(
    store: SQLiteJobStore,
    *,
    job_id: str = "job-1",
    run_id: str = "run-1",
    started_at: datetime = NOW,
) -> None:
    claimed = await store.claim_run(
        run_id,
        job_id,
        "adhoc",
        "config-hash",
        started_at,
    )
    assert claimed is True


async def test_claim_and_heartbeat_compare_and_set(store: SQLiteJobStore) -> None:
    await _save_queued(store)
    await _claim(store)
    heartbeat_at = NOW + timedelta(seconds=30)

    await store.heartbeat_run("job-1", "run-1", heartbeat_at)

    run = await store.get_job_run("job-1", "run-1")
    assert run is not None
    assert run.status == JobStatus.running
    assert run.started_at == NOW
    assert run.heartbeat_at == heartbeat_at


async def test_heartbeat_rejects_wrong_run_id(store: SQLiteJobStore) -> None:
    await _save_queued(store)
    await _claim(store)

    with pytest.raises(RunOwnershipError, match="heartbeat"):
        await store.heartbeat_run("job-1", "run-other", NOW + timedelta(seconds=30))

    run = await store.get_job_run("job-1", "run-1")
    assert run is not None
    assert run.heartbeat_at == NOW


async def test_heartbeat_rejects_terminal_and_superseded_runs(
    store: SQLiteJobStore,
) -> None:
    await _save_queued(store, job_id="terminal")
    await _claim(store, job_id="terminal")
    await store.finalize_owned_run(
        "terminal",
        "run-1",
        "complete",
        2,
        0,
        NOW + timedelta(seconds=10),
        NOW - timedelta(seconds=1),
    )
    with pytest.raises(RunOwnershipError):
        await store.heartbeat_run("terminal", "run-1", NOW + timedelta(seconds=30))

    await _save_queued(store, job_id="superseded", run_id="run-old")
    await _claim(store, job_id="superseded", run_id="run-old")
    job = await store.get_job("superseded")
    await store.update_job_status(job.model_copy(update={"current_run_id": "run-new"}))
    with pytest.raises(RunOwnershipError):
        await store.heartbeat_run("superseded", "run-old", NOW + timedelta(seconds=30))


async def test_conditional_finalization_updates_run_and_job_together(
    store: SQLiteJobStore,
) -> None:
    await _save_queued(store)
    await _claim(store)
    completed_at = NOW + timedelta(minutes=2)

    await store.finalize_owned_run(
        "job-1",
        "run-1",
        "partial",
        7,
        2,
        completed_at,
        NOW - timedelta(seconds=1),
    )

    job = await store.get_job("job-1")
    run = await store.get_job_run("job-1", "run-1")
    assert job.status == JobStatus.partial
    assert job.current_run_id == "run-1"
    assert run is not None
    assert run.status == JobStatus.partial
    assert run.completed_at == completed_at
    assert run.record_count == 7
    assert run.error_count == 2


async def test_finalization_rejects_stale_heartbeat_without_partial_mutation(
    store: SQLiteJobStore,
) -> None:
    await _save_queued(store)
    await _claim(store)

    with pytest.raises(RunOwnershipError, match="finalize"):
        await store.finalize_owned_run(
            "job-1",
            "run-1",
            "complete",
            1,
            0,
            NOW + timedelta(minutes=20),
            NOW + timedelta(minutes=10),
        )

    assert (await store.get_job("job-1")).status == JobStatus.running
    run = await store.get_job_run("job-1", "run-1")
    assert run is not None
    assert run.status == JobStatus.running


async def test_stale_recovery_is_conditional_and_idempotent(
    store: SQLiteJobStore,
) -> None:
    await _save_queued(store)
    await _claim(store)
    cutoff = NOW + timedelta(minutes=10)
    recovered_at = NOW + timedelta(minutes=11)

    assert await store.recover_stale_run("job-1", "run-1", cutoff, recovered_at)
    assert not await store.recover_stale_run("job-1", "run-1", cutoff, recovered_at)

    job = await store.get_job("job-1")
    run = await store.get_job_run("job-1", "run-1")
    assert job.status == JobStatus.failed
    assert run is not None
    assert run.status == JobStatus.failed
    assert run.completed_at == recovered_at


async def test_fresh_heartbeat_prevents_recovery(store: SQLiteJobStore) -> None:
    await _save_queued(store)
    await _claim(store)
    fresh_at = NOW + timedelta(minutes=9)
    await store.heartbeat_run("job-1", "run-1", fresh_at)

    recovered = await store.recover_stale_run(
        "job-1",
        "run-1",
        NOW + timedelta(minutes=8),
        NOW + timedelta(minutes=10),
    )

    assert recovered is False
    assert (await store.get_job("job-1")).status == JobStatus.running


async def test_claim_insert_failure_rolls_back_job_transition(
    store: SQLiteJobStore,
) -> None:
    await _save_queued(store)
    async with get_db("jobs.db") as db:
        await db.executescript(
            """CREATE TRIGGER reject_test_run
               BEFORE INSERT ON job_runs
               BEGIN
                   SELECT RAISE(ABORT, 'test run insert failure');
               END;"""
        )
        await db.commit()

    with pytest.raises(aiosqlite.IntegrityError, match="test run insert failure"):
        await store.claim_run("run-1", "job-1", "adhoc", "hash", NOW)

    job = await store.get_job("job-1")
    assert job.status == JobStatus.queued
    assert await store.get_job_run("job-1", "run-1") is None


async def test_heartbeat_and_recovery_race_has_one_consistent_winner(
    store: SQLiteJobStore,
) -> None:
    await _save_queued(store)
    await _claim(store)
    fresh_at = NOW + timedelta(minutes=9)

    results = await asyncio.gather(
        store.recover_stale_run(
            "job-1",
            "run-1",
            NOW + timedelta(minutes=8),
            NOW + timedelta(minutes=10),
        ),
        store.heartbeat_run("job-1", "run-1", fresh_at),
        return_exceptions=True,
    )

    job = await store.get_job("job-1")
    run = await store.get_job_run("job-1", "run-1")
    assert run is not None
    if results[0] is True:
        assert isinstance(results[1], RunOwnershipError)
        assert job.status == run.status == JobStatus.failed
    else:
        assert results == [False, None]
        assert job.status == run.status == JobStatus.running
        assert run.heartbeat_at == fresh_at


async def test_heartbeat_and_finalization_race_has_one_consistent_winner(
    store: SQLiteJobStore,
) -> None:
    await _save_queued(store)
    await _claim(store)
    fresh_at = NOW + timedelta(seconds=9)

    results = await asyncio.gather(
        store.finalize_owned_run(
            "job-1",
            "run-1",
            "complete",
            1,
            0,
            NOW + timedelta(seconds=10),
            NOW + timedelta(seconds=8),
        ),
        store.heartbeat_run("job-1", "run-1", fresh_at),
        return_exceptions=True,
    )

    job = await store.get_job("job-1")
    run = await store.get_job_run("job-1", "run-1")
    assert run is not None
    if results[0] is None:
        assert results[1] is None
        assert job.status == run.status == JobStatus.complete
    else:
        assert isinstance(results[0], RunOwnershipError)
        assert results[1] is None
        assert job.status == run.status == JobStatus.running
        assert run.heartbeat_at == fresh_at


async def test_recovery_and_finalization_race_has_one_terminal_winner(
    store: SQLiteJobStore,
) -> None:
    await _save_queued(store)
    await _claim(store)

    results = await asyncio.gather(
        store.recover_stale_run(
            "job-1",
            "run-1",
            NOW + timedelta(seconds=1),
            NOW + timedelta(seconds=10),
        ),
        store.finalize_owned_run(
            "job-1",
            "run-1",
            "complete",
            1,
            0,
            NOW + timedelta(seconds=9),
            NOW - timedelta(seconds=1),
        ),
        return_exceptions=True,
    )

    job = await store.get_job("job-1")
    run = await store.get_job_run("job-1", "run-1")
    assert run is not None
    if results[0] is True:
        assert isinstance(results[1], RunOwnershipError)
        assert job.status == run.status == JobStatus.failed
    else:
        assert results == [False, None]
        assert job.status == run.status == JobStatus.complete
