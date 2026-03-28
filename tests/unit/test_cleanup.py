"""Test result retention auto-cleanup."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import scrapeyard.storage.cleanup as cleanup_module
from scrapeyard.storage.cleanup import run_cleanup
from scrapeyard.storage.database import get_db, init_db, reset_db


@pytest.fixture
async def db_and_dirs(tmp_path):
    """Set up DB and results directory for cleanup tests."""
    db_dir = tmp_path / "db"
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    await init_db(str(db_dir))
    yield results_dir
    reset_db()


def _create_result_on_disk(results_dir: Path, project: str, job_name: str, run_id: str) -> Path:
    """Create a fake result directory with a JSON file."""
    run_dir = results_dir / project / job_name / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "results.json").write_text(json.dumps([{"data": "test"}]))
    return run_dir


async def _insert_meta(
    job_id: str,
    project: str,
    run_id: str,
    file_path: str,
    created_at: str,
) -> None:
    """Insert a row into results_meta directly."""
    async with get_db("results_meta.db") as db:
        await db.execute(
            """INSERT INTO results_meta
               (job_id, project, run_id, status, record_count, file_path, created_at)
               VALUES (?, ?, ?, 'complete', 1, ?, ?)""",
            (job_id, project, run_id, file_path, created_at),
        )
        await db.commit()


async def _run_to_thread(func, *args, **kwargs):
    return func(*args, **kwargs)


@pytest.mark.asyncio
async def test_run_cleanup_delegates_age_based_deletion(db_and_dirs):
    result_store = AsyncMock()
    result_store.delete_expired = AsyncMock(return_value=1)

    async with get_db("results_meta.db") as db:
        await run_cleanup(result_store, retention_days=30, max_results_per_job=100, db=db)

    result_store.delete_expired.assert_awaited_once_with(30)


@pytest.mark.asyncio
async def test_run_cleanup_prunes_excess_results_per_job(db_and_dirs):
    results_dir = db_and_dirs
    now = datetime.now(timezone.utc)
    result_store = AsyncMock()
    result_store.delete_expired = AsyncMock(return_value=0)

    # Create 3 runs for the same job, all fresh.
    run_dirs = []
    for i in range(3):
        ts = (now - timedelta(hours=3 - i)).isoformat()
        run_id = f"run-{i}"
        run_dir = _create_result_on_disk(results_dir, "proj", "job1", run_id)
        run_dirs.append(run_dir)
        await _insert_meta("job-1", "proj", run_id, str(run_dir), ts)

    # max_results_per_job=2 should delete the oldest (run-0).
    async with get_db("results_meta.db") as db:
        await run_cleanup(result_store, retention_days=30, max_results_per_job=2, db=db)

        cursor = await db.execute(
            "SELECT run_id FROM results_meta WHERE job_id = ? ORDER BY created_at",
            ("job-1",),
        )
        remaining = [r[0] for r in await cursor.fetchall()]

    assert remaining == ["run-1", "run-2"]
    assert not run_dirs[0].exists()
    assert run_dirs[1].exists()
    assert run_dirs[2].exists()


@pytest.mark.asyncio
async def test_run_cleanup_prunes_per_job_independently(db_and_dirs):
    results_dir = db_and_dirs
    now = datetime.now(timezone.utc)
    result_store = AsyncMock()
    result_store.delete_expired = AsyncMock(return_value=0)

    # Job A: 3 runs, Job B: 1 run. max=2 should only prune Job A.
    for i in range(3):
        ts = (now - timedelta(hours=3 - i)).isoformat()
        run_dir = _create_result_on_disk(results_dir, "proj", "jobA", f"runA-{i}")
        await _insert_meta("job-A", "proj", f"runA-{i}", str(run_dir), ts)

    run_dir_b = _create_result_on_disk(results_dir, "proj", "jobB", "runB-0")
    await _insert_meta("job-B", "proj", "runB-0", str(run_dir_b), now.isoformat())

    async with get_db("results_meta.db") as db:
        await run_cleanup(result_store, retention_days=30, max_results_per_job=2, db=db)

        cursor = await db.execute("SELECT COUNT(*) FROM results_meta WHERE job_id = ?", ("job-A",))
        count_a = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT COUNT(*) FROM results_meta WHERE job_id = ?", ("job-B",))
        count_b = (await cursor.fetchone())[0]

    assert count_a == 2
    assert count_b == 1


@pytest.mark.asyncio
async def test_run_cleanup_offloads_prune_directory_removal(db_and_dirs):
    results_dir = db_and_dirs
    now = datetime.now(timezone.utc)
    result_store = AsyncMock()
    result_store.delete_expired = AsyncMock(return_value=0)

    old_run_dir = _create_result_on_disk(results_dir, "proj", "job1", "run-0")
    await _insert_meta(
        "job-1",
        "proj",
        "run-0",
        str(old_run_dir),
        (now - timedelta(hours=3)).isoformat(),
    )
    for index in (1, 2):
        run_dir = _create_result_on_disk(results_dir, "proj", "job1", f"run-{index}")
        await _insert_meta(
            "job-1",
            "proj",
            f"run-{index}",
            str(run_dir),
            (now - timedelta(hours=2 - index)).isoformat(),
        )

    with patch.object(
        cleanup_module.asyncio,
        "to_thread",
        new_callable=AsyncMock,
    ) as mock_to_thread:
        mock_to_thread.side_effect = _run_to_thread
        async with get_db("results_meta.db") as db:
            await run_cleanup(
                result_store,
                retention_days=30,
                max_results_per_job=2,
                db=db,
            )

    assert mock_to_thread.await_args.args[0] is cleanup_module.remove_directories
    assert mock_to_thread.await_args.args[1] == [str(old_run_dir)]
