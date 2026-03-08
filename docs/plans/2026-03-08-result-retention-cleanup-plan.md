# Result Retention Auto-Cleanup Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a `run_cleanup` coroutine that removes expired results and prunes runs exceeding `max_results_per_job`, plus update `start_cleanup_loop` to use it.

**Architecture:** `run_cleanup` operates directly on `results_meta.db` and the filesystem. `start_cleanup_loop` is updated to call `run_cleanup` with settings from `get_settings()`. The lifespan in `main.py` is simplified to call the no-arg `start_cleanup_loop()`.

**Tech Stack:** aiosqlite, asyncio, shutil, pytest

---

### Task 1: Write tests for `run_cleanup`

**Files:**
- Create: `tests/unit/test_cleanup.py`

**Step 1: Write the test file**

Create `tests/unit/test_cleanup.py`:

```python
"""Test result retention auto-cleanup."""

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

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
               (job_id, project, run_id, status, record_count, file_path, format, created_at)
               VALUES (?, ?, ?, 'complete', 1, ?, 'json', ?)""",
            (job_id, project, run_id, file_path, created_at),
        )
        await db.commit()


@pytest.mark.asyncio
async def test_run_cleanup_removes_expired_results(db_and_dirs):
    results_dir = db_and_dirs
    old_date = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    run_dir = _create_result_on_disk(results_dir, "proj", "job1", "run-old")

    await _insert_meta("job-1", "proj", "run-old", str(run_dir), old_date)

    async with get_db("results_meta.db") as db:
        await run_cleanup(str(results_dir), retention_days=30, max_results_per_job=100, db=db)

        cursor = await db.execute("SELECT COUNT(*) FROM results_meta WHERE run_id = ?", ("run-old",))
        row = await cursor.fetchone()

    assert row[0] == 0
    assert not run_dir.exists()


@pytest.mark.asyncio
async def test_run_cleanup_keeps_fresh_results(db_and_dirs):
    results_dir = db_and_dirs
    now = datetime.now(timezone.utc).isoformat()
    run_dir = _create_result_on_disk(results_dir, "proj", "job1", "run-fresh")

    await _insert_meta("job-1", "proj", "run-fresh", str(run_dir), now)

    async with get_db("results_meta.db") as db:
        await run_cleanup(str(results_dir), retention_days=30, max_results_per_job=100, db=db)

        cursor = await db.execute("SELECT COUNT(*) FROM results_meta WHERE run_id = ?", ("run-fresh",))
        row = await cursor.fetchone()

    assert row[0] == 1
    assert run_dir.exists()


@pytest.mark.asyncio
async def test_run_cleanup_prunes_excess_results_per_job(db_and_dirs):
    results_dir = db_and_dirs
    now = datetime.now(timezone.utc)

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
        await run_cleanup(str(results_dir), retention_days=30, max_results_per_job=2, db=db)

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

    # Job A: 3 runs, Job B: 1 run. max=2 should only prune Job A.
    for i in range(3):
        ts = (now - timedelta(hours=3 - i)).isoformat()
        run_dir = _create_result_on_disk(results_dir, "proj", "jobA", f"runA-{i}")
        await _insert_meta("job-A", "proj", f"runA-{i}", str(run_dir), ts)

    run_dir_b = _create_result_on_disk(results_dir, "proj", "jobB", "runB-0")
    await _insert_meta("job-B", "proj", "runB-0", str(run_dir_b), now.isoformat())

    async with get_db("results_meta.db") as db:
        await run_cleanup(str(results_dir), retention_days=30, max_results_per_job=2, db=db)

        cursor = await db.execute("SELECT COUNT(*) FROM results_meta WHERE job_id = ?", ("job-A",))
        count_a = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT COUNT(*) FROM results_meta WHERE job_id = ?", ("job-B",))
        count_b = (await cursor.fetchone())[0]

    assert count_a == 2
    assert count_b == 1
```

**Step 2: Run tests to verify they fail**

Run: `source .venv/bin/activate && pytest tests/unit/test_cleanup.py -v`
Expected: FAIL — `ImportError: cannot import name 'run_cleanup'`

**Step 3: Commit test file**

```bash
git add tests/unit/test_cleanup.py
git commit -m "test: add tests for run_cleanup coroutine"
```

---

### Task 2: Implement `run_cleanup` and update `start_cleanup_loop`

**Files:**
- Modify: `src/scrapeyard/storage/cleanup.py`

**Step 1: Rewrite `cleanup.py`**

Replace the entire content of `src/scrapeyard/storage/cleanup.py` with:

```python
"""Periodic cleanup of expired scrape results."""

from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from scrapeyard.common.settings import get_settings
from scrapeyard.storage.database import get_db

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_HOURS = 6


async def run_cleanup(
    results_dir: str,
    retention_days: int,
    max_results_per_job: int,
    db: aiosqlite.Connection,
) -> None:
    """Remove expired result files and prune runs exceeding the per-job limit.

    Operates directly on the database and filesystem.

    Parameters
    ----------
    results_dir:
        Root directory where result files are stored.
    retention_days:
        Delete results older than this many days.
    max_results_per_job:
        Maximum number of result runs to keep per job (most recent kept).
    db:
        An open ``aiosqlite.Connection`` to ``results_meta.db``.
    """
    # 1. Age-based cleanup: delete results older than retention_days.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    cursor = await db.execute(
        "SELECT id, file_path FROM results_meta WHERE created_at < ?",
        (cutoff,),
    )
    expired_rows = await cursor.fetchall()
    for row_id, file_path in expired_rows:
        run_dir = Path(file_path)
        if run_dir.exists():
            shutil.rmtree(run_dir)
    if expired_rows:
        ids = [r[0] for r in expired_rows]
        placeholders = ",".join("?" for _ in ids)
        await db.execute(
            f"DELETE FROM results_meta WHERE id IN ({placeholders})",
            ids,
        )
        await db.commit()
        logger.info("Cleanup removed %d expired result(s)", len(expired_rows))

    # 2. Per-job pruning: keep only max_results_per_job most recent runs per job.
    cursor = await db.execute(
        "SELECT DISTINCT job_id FROM results_meta",
    )
    job_ids = [r[0] for r in await cursor.fetchall()]

    pruned_total = 0
    for job_id in job_ids:
        cursor = await db.execute(
            "SELECT id, file_path FROM results_meta WHERE job_id = ? ORDER BY created_at DESC",
            (job_id,),
        )
        rows = await cursor.fetchall()
        if len(rows) <= max_results_per_job:
            continue
        excess = rows[max_results_per_job:]
        for row_id, file_path in excess:
            run_dir = Path(file_path)
            if run_dir.exists():
                shutil.rmtree(run_dir)
        excess_ids = [r[0] for r in excess]
        placeholders = ",".join("?" for _ in excess_ids)
        await db.execute(
            f"DELETE FROM results_meta WHERE id IN ({placeholders})",
            excess_ids,
        )
        pruned_total += len(excess)

    if pruned_total:
        await db.commit()
        logger.info("Cleanup pruned %d excess result(s) across jobs", pruned_total)


def start_cleanup_loop(interval_hours: float = _DEFAULT_INTERVAL_HOURS) -> asyncio.Task:
    """Spawn a background task that periodically runs cleanup.

    Reads settings from :func:`get_settings` and obtains a database
    connection via :func:`get_db` on each iteration.

    Returns the :class:`asyncio.Task` so the caller can cancel it on shutdown.
    """
    settings = get_settings()

    async def _loop() -> None:
        while True:
            try:
                async with get_db("results_meta.db") as db:
                    await run_cleanup(
                        results_dir=settings.storage_results_dir,
                        retention_days=settings.storage_retention_days,
                        max_results_per_job=settings.storage_max_results_per_job,
                        db=db,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error during result cleanup")
            await asyncio.sleep(interval_hours * 3600)

    return asyncio.create_task(_loop(), name="result-cleanup")
```

**Step 2: Run tests to verify they pass**

Run: `source .venv/bin/activate && pytest tests/unit/test_cleanup.py -v`
Expected: All 4 PASS

**Step 3: Commit**

```bash
git add src/scrapeyard/storage/cleanup.py
git commit -m "feat: implement run_cleanup with age and per-job pruning"
```

---

### Task 3: Update lifespan and fix existing cleanup loop tests

**Files:**
- Modify: `src/scrapeyard/main.py:53-57`
- Modify: `tests/unit/test_cleanup_loop.py`

**Step 1: Update the lifespan call in `main.py`**

Replace lines 53-57:

```python
    # 6. Cleanup loop
    app.state.cleanup_task = start_cleanup_loop(
        app.state.result_store,
        retention_days=settings.storage_retention_days,
    )
```

With:

```python
    # 6. Cleanup loop
    app.state.cleanup_task = start_cleanup_loop()
```

**Step 2: Update existing cleanup loop tests**

Replace the entire content of `tests/unit/test_cleanup_loop.py` with:

```python
"""Test the result retention cleanup loop."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from scrapeyard.storage.cleanup import start_cleanup_loop


@pytest.mark.asyncio
async def test_cleanup_loop_runs_periodically():
    mock_run_cleanup = AsyncMock()

    with patch("scrapeyard.storage.cleanup.run_cleanup", mock_run_cleanup), \
         patch("scrapeyard.storage.cleanup.get_db") as mock_get_db:
        # Make get_db return an async context manager yielding a mock connection.
        mock_conn = AsyncMock()
        mock_get_db.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_get_db.return_value.__aexit__ = AsyncMock(return_value=False)

        task = start_cleanup_loop(interval_hours=0.05 / 3600)
        await asyncio.sleep(0.15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert mock_run_cleanup.call_count >= 2


@pytest.mark.asyncio
async def test_cleanup_loop_handles_cancellation():
    mock_run_cleanup = AsyncMock()

    with patch("scrapeyard.storage.cleanup.run_cleanup", mock_run_cleanup), \
         patch("scrapeyard.storage.cleanup.get_db") as mock_get_db:
        mock_conn = AsyncMock()
        mock_get_db.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_get_db.return_value.__aexit__ = AsyncMock(return_value=False)

        task = start_cleanup_loop(interval_hours=0.05 / 3600)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
```

**Step 3: Run all tests**

Run: `source .venv/bin/activate && pytest tests/unit/test_cleanup.py tests/unit/test_cleanup_loop.py tests/unit/test_health.py -v`
Expected: All PASS

**Step 4: Commit**

```bash
git add src/scrapeyard/main.py tests/unit/test_cleanup_loop.py
git commit -m "refactor: update start_cleanup_loop to use run_cleanup, simplify lifespan"
```

---

### Task 4: Lint and final verification

**Files:** None (verification only)

**Step 1: Run ruff**

Run: `source .venv/bin/activate && ruff check src/scrapeyard/storage/cleanup.py src/scrapeyard/main.py`
Expected: All checks passed

**Step 2: Run full test suite**

Run: `source .venv/bin/activate && pytest tests/ -v`
Expected: All PASS

**Step 3: Run specific acceptance test**

Run: `source .venv/bin/activate && pytest tests/unit/test_cleanup.py -v`
Expected: All PASS

**Step 4: Commit if any fixes were needed**

```bash
git add -u
git commit -m "chore: lint fixes for result retention cleanup"
```
