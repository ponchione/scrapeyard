"""Test result retention cleanup."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, call, patch

import pytest

import scrapeyard.storage.result_store as result_store_module
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


async def _run_to_thread(func, *args, **kwargs):
    return func(*args, **kwargs)


@pytest.mark.asyncio
async def test_delete_expired_removes_old_results(store, tmp_path):
    # Save a result.
    meta = await store.save_result("job-1", [{"url": "http://example.com"}])
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
    meta = await store.save_result("job-2", [{"url": "http://example.com"}])
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


@pytest.mark.asyncio
async def test_delete_expired_offloads_directory_removal(store):
    meta = await store.save_result("job-3", [{"url": "http://example.com"}])
    run_id = meta.run_id

    from scrapeyard.storage.database import get_db

    old_date = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    async with get_db("results_meta.db") as db:
        await db.execute(
            "UPDATE results_meta SET created_at = ? WHERE run_id = ?",
            (old_date, run_id),
        )
        await db.commit()

    with patch.object(
        result_store_module.asyncio,
        "to_thread",
        new_callable=AsyncMock,
    ) as mock_to_thread:
        mock_to_thread.side_effect = _run_to_thread
        deleted = await store.delete_expired(30)

    assert deleted == 1
    assert mock_to_thread.await_args == call(
        result_store_module.remove_directories,
        [meta.file_path],
    )


@pytest.mark.asyncio
async def test_prune_excess_per_job_removes_oldest_runs(store):
    from scrapeyard.storage.database import get_db

    run_ids = []
    for index in range(3):
        meta = await store.save_result(
            "job-prune",
            [{"url": f"http://example.com/{index}"}],
            run_id=f"run-{index}",
        )
        run_ids.append(meta.run_id)

    async with get_db("results_meta.db") as db:
        for index, run_id in enumerate(run_ids):
            created_at = (datetime.now(timezone.utc) - timedelta(hours=3 - index)).isoformat()
            await db.execute(
                "UPDATE results_meta SET created_at = ? WHERE run_id = ?",
                (created_at, run_id),
            )
        await db.commit()

    deleted = await store.prune_excess_per_job(2)

    assert deleted == 1
    with pytest.raises(KeyError):
        await store.get_result("job-prune", run_id="run-0")
    payload = await store.get_result("job-prune", run_id="run-2")
    assert payload.run_id == "run-2"


@pytest.mark.asyncio
async def test_prune_excess_per_job_offloads_directory_removal(store):
    from scrapeyard.storage.database import get_db

    oldest = await store.save_result(
        "job-prune-thread",
        [{"url": "http://example.com/old"}],
        run_id="run-old",
    )
    await store.save_result(
        "job-prune-thread",
        [{"url": "http://example.com/new"}],
        run_id="run-new",
    )

    async with get_db("results_meta.db") as db:
        await db.execute(
            "UPDATE results_meta SET created_at = ? WHERE run_id = ?",
            ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(), "run-old"),
        )
        await db.execute(
            "UPDATE results_meta SET created_at = ? WHERE run_id = ?",
            ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), "run-new"),
        )
        await db.commit()

    with patch.object(
        result_store_module.asyncio,
        "to_thread",
        new_callable=AsyncMock,
    ) as mock_to_thread:
        mock_to_thread.side_effect = _run_to_thread
        deleted = await store.prune_excess_per_job(1)

    assert deleted == 1
    assert mock_to_thread.await_args == call(
        result_store_module.remove_directories,
        [oldest.file_path],
    )
