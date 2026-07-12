from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from scrapeyard.models.job import Job
from scrapeyard.storage.database import get_db, init_db
from scrapeyard.storage.job_store import SQLiteJobStore
from scrapeyard.storage.types import IdempotentJobAction


@pytest.fixture()
async def store(tmp_path):
    await init_db(str(tmp_path / "db"))
    return SQLiteJobStore()


def _job(index: int, *, created_at: datetime) -> Job:
    return Job(
        job_id=f"job-{index}",
        project="idem",
        name=f"request-{index}",
        config_yaml="project: idem",
        current_run_id=f"run-{index}",
        created_at=created_at,
    )


async def _create(
    store: SQLiteJobStore,
    index: int,
    *,
    created_at: datetime,
    caller_scope: str = "caller-a",
    request_hash: str = "request-hash",
):
    return await store.create_idempotent_job(
        _job(index, created_at=created_at),
        caller_scope=caller_scope,
        key_digest=hashlib.sha256(b"sentinel-client-key").hexdigest(),
        request_hash=request_hash,
        response_mode="async",
        expires_at=created_at + timedelta(hours=24),
    )


async def test_concurrent_identical_creates_have_one_winner(store):
    now = datetime(2026, 7, 12, tzinfo=timezone.utc)

    outcomes = await asyncio.gather(
        *(_create(store, index, created_at=now) for index in range(20))
    )

    assert [outcome.action for outcome in outcomes].count(
        IdempotentJobAction.created
    ) == 1
    assert [outcome.action for outcome in outcomes].count(
        IdempotentJobAction.matched
    ) == 19
    assert len({outcome.job.job_id for outcome in outcomes}) == 1
    assert len({outcome.run_id for outcome in outcomes}) == 1
    async with get_db("jobs.db") as db:
        jobs = await (await db.execute("SELECT COUNT(*) FROM jobs")).fetchone()
        records = await (
            await db.execute("SELECT COUNT(*) FROM scrape_idempotency")
        ).fetchone()
    assert jobs[0] == 1
    assert records[0] == 1


async def test_same_key_with_different_request_conflicts_without_new_job(store):
    now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    original = await _create(store, 1, created_at=now)
    conflict = await _create(
        store,
        2,
        created_at=now + timedelta(seconds=1),
        request_hash="different-request-hash",
    )

    assert original.action is IdempotentJobAction.created
    assert conflict.action is IdempotentJobAction.conflict
    assert conflict.job.job_id == original.job.job_id
    with pytest.raises(KeyError):
        await store.get_job("job-2")


async def test_keys_are_caller_scoped_and_plaintext_key_is_not_stored(store):
    now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    first = await _create(store, 1, created_at=now, caller_scope="caller-a")
    second = await _create(store, 2, created_at=now, caller_scope="caller-b")

    assert first.action is IdempotentJobAction.created
    assert second.action is IdempotentJobAction.created
    async with get_db("jobs.db") as db:
        cursor = await db.execute(
            "SELECT caller_scope, key_digest FROM scrape_idempotency ORDER BY caller_scope"
        )
        rows = await cursor.fetchall()
        raw = (await db.execute("SELECT quote(key_digest) FROM scrape_idempotency"))
        stored = " ".join(str(row[0]) for row in await raw.fetchall())
    assert [row[0] for row in rows] == ["caller-a", "caller-b"]
    assert "sentinel-client-key" not in stored


async def test_expired_key_is_reusable_and_periodic_cleanup_is_bounded(store):
    now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    await _create(store, 1, created_at=now)

    replacement = await _create(store, 2, created_at=now + timedelta(hours=25))
    assert replacement.action is IdempotentJobAction.created
    assert replacement.job.job_id == "job-2"

    await _create(
        store,
        3,
        created_at=now,
        caller_scope="expired-b",
    )
    await _create(
        store,
        4,
        created_at=now,
        caller_scope="expired-c",
    )
    deleted = await store.delete_expired_idempotency_records(
        now + timedelta(hours=25),
        limit=1,
    )
    assert deleted == 1
    async with get_db("jobs.db") as db:
        remaining = await (
            await db.execute("SELECT COUNT(*) FROM scrape_idempotency")
        ).fetchone()
    assert remaining[0] == 2


async def test_enqueue_rollback_cascades_idempotency_record(store):
    now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    outcome = await _create(store, 1, created_at=now)

    assert await store.rollback_queued_submission(
        outcome.job.job_id,
        outcome.run_id,
    )
    async with get_db("jobs.db") as db:
        count = await (
            await db.execute("SELECT COUNT(*) FROM scrape_idempotency")
        ).fetchone()
    assert count[0] == 0
