"""Test result retention cleanup."""
from datetime import datetime, timedelta, timezone

import pytest

from scrapeyard.storage.database import init_db, reset_db
from scrapeyard.storage.result_store import LocalResultStore


@pytest.fixture
async def store(tmp_path):
    db_dir = tmp_path / "db"
    results_dir = tmp_path / "results"
    await init_db(str(db_dir))

    async def _lookup(job_id: str) -> tuple[str, str]:
        return ("test-project", "test-job")

    store = LocalResultStore(str(results_dir), _lookup)
    yield store
    reset_db()


@pytest.mark.asyncio
async def test_delete_expired_removes_old_results(store, tmp_path):
    # Save a result.
    meta = await store.save_result("job-1", [{"url": "http://example.com"}], "json")
    run_id = meta.run_id

    # Manually backdate the created_at to 31 days ago.
    from scrapeyard.storage.database import get_db

    old_date = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    async with get_db("results_meta.db") as db:
        await db.execute(
            "UPDATE results_meta SET created_at = ? WHERE run_id = ?",
            (old_date, run_id),
        )
        await db.commit()

    deleted = await store.delete_expired(30)
    assert deleted >= 1

    # Verify the result is gone from DB.
    async with get_db("results_meta.db") as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM results_meta WHERE run_id = ?", (run_id,)
        )
        row = await cursor.fetchone()
    assert row[0] == 0


@pytest.mark.asyncio
async def test_delete_expired_keeps_fresh_results(store):
    meta = await store.save_result("job-2", [{"url": "http://example.com"}], "json")
    run_id = meta.run_id
    deleted = await store.delete_expired(30)
    assert deleted == 0

    # Verify the result is still in DB.
    from scrapeyard.storage.database import get_db

    async with get_db("results_meta.db") as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM results_meta WHERE run_id = ?", (run_id,)
        )
        row = await cursor.fetchone()
    assert row[0] == 1
