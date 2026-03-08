# FastAPI Lifespan Wiring — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Wire up the FastAPI lifespan context manager to orchestrate all startup initialization and graceful shutdown in the correct dependency order.

**Architecture:** Enhance the existing `lifespan()` in `main.py` to call `init_db`, create/store storage instances on `app.state`, and start a new result retention cleanup loop. Add `reset_db()` to `database.py`, `delete_expired()` to `LocalResultStore`, and a new `cleanup.py` module. Fix test fixtures to provide a temp DB directory.

**Tech Stack:** FastAPI, aiosqlite, asyncio, pytest, httpx

---

### Task 1: Add `reset_db()` to database module

**Files:**
- Modify: `src/scrapeyard/storage/database.py:20-21`
- Test: `tests/unit/test_database_reset.py`

**Step 1: Write the failing test**

Create `tests/unit/test_database_reset.py`:

```python
"""Test database reset function."""

import pytest

from scrapeyard.storage.database import _db_dir, init_db, reset_db


@pytest.mark.asyncio
async def test_reset_db_clears_state(tmp_path):
    await init_db(str(tmp_path / "db"))
    from scrapeyard.storage import database

    assert database._db_dir is not None
    reset_db()
    assert database._db_dir is None
```

**Step 2: Run test to verify it fails**

Run: `source .venv/bin/activate && pytest tests/unit/test_database_reset.py -v`
Expected: FAIL — `ImportError: cannot import name 'reset_db'`

**Step 3: Write minimal implementation**

Add to `src/scrapeyard/storage/database.py` after the `_db_dir` declaration (after line 20):

```python
def reset_db() -> None:
    """Clear the module-level database directory, signalling teardown."""
    global _db_dir  # noqa: PLW0603
    _db_dir = None
```

**Step 4: Run test to verify it passes**

Run: `source .venv/bin/activate && pytest tests/unit/test_database_reset.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/scrapeyard/storage/database.py tests/unit/test_database_reset.py
git commit -m "feat: add reset_db() for clean lifespan teardown"
```

---

### Task 2: Add `delete_expired()` to ResultStore protocol and LocalResultStore

**Files:**
- Modify: `src/scrapeyard/storage/protocols.py:24-31`
- Modify: `src/scrapeyard/storage/result_store.py`
- Test: `tests/unit/test_result_store_cleanup.py`

**Step 1: Write the failing test**

Create `tests/unit/test_result_store_cleanup.py`:

```python
"""Test result retention cleanup."""

import json
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
    run_id = await store.save_result("job-1", [{"url": "http://example.com"}], "json")

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
    run_id = await store.save_result("job-2", [{"url": "http://example.com"}], "json")
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
```

**Step 2: Run test to verify it fails**

Run: `source .venv/bin/activate && pytest tests/unit/test_result_store_cleanup.py -v`
Expected: FAIL — `AttributeError: 'LocalResultStore' object has no attribute 'delete_expired'`

**Step 3: Add `delete_expired` to the protocol**

In `src/scrapeyard/storage/protocols.py`, add to the `ResultStore` class after `delete_results`:

```python
    async def delete_expired(self, retention_days: int) -> int: ...
```

**Step 4: Implement `delete_expired` in LocalResultStore**

Add to `src/scrapeyard/storage/result_store.py` at the end of the `LocalResultStore` class:

```python
    async def delete_expired(self, retention_days: int) -> int:
        """Delete results older than *retention_days*. Returns count deleted."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=retention_days)
        ).isoformat()
        async with get_db("results_meta.db") as db:
            cursor = await db.execute(
                "SELECT id, file_path FROM results_meta WHERE created_at < ?",
                (cutoff,),
            )
            rows = await cursor.fetchall()
            for row_id, file_path in rows:
                run_dir = Path(file_path)
                if run_dir.exists():
                    shutil.rmtree(run_dir)
            if rows:
                ids = [r[0] for r in rows]
                placeholders = ",".join("?" for _ in ids)
                await db.execute(
                    f"DELETE FROM results_meta WHERE id IN ({placeholders})",
                    ids,
                )
                await db.commit()
        return len(rows)
```

Note: `timedelta` must be imported — it's already imported in the file.

**Step 5: Run test to verify it passes**

Run: `source .venv/bin/activate && pytest tests/unit/test_result_store_cleanup.py -v`
Expected: PASS

**Step 6: Commit**

```bash
git add src/scrapeyard/storage/protocols.py src/scrapeyard/storage/result_store.py tests/unit/test_result_store_cleanup.py
git commit -m "feat: add delete_expired to ResultStore for retention cleanup"
```

---

### Task 3: Create `cleanup.py` with `start_cleanup_loop`

**Files:**
- Create: `src/scrapeyard/storage/cleanup.py`
- Test: `tests/unit/test_cleanup_loop.py`

**Step 1: Write the failing test**

Create `tests/unit/test_cleanup_loop.py`:

```python
"""Test the result retention cleanup loop."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from scrapeyard.storage.cleanup import start_cleanup_loop


@pytest.mark.asyncio
async def test_cleanup_loop_calls_delete_expired():
    mock_store = AsyncMock()
    mock_store.delete_expired = AsyncMock(return_value=0)

    task = start_cleanup_loop(mock_store, retention_days=30, interval_seconds=0.05)
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert mock_store.delete_expired.call_count >= 2
    mock_store.delete_expired.assert_called_with(30)


@pytest.mark.asyncio
async def test_cleanup_loop_handles_cancellation():
    mock_store = AsyncMock()
    mock_store.delete_expired = AsyncMock(return_value=0)

    task = start_cleanup_loop(mock_store, retention_days=7, interval_seconds=0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
```

**Step 2: Run test to verify it fails**

Run: `source .venv/bin/activate && pytest tests/unit/test_cleanup_loop.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scrapeyard.storage.cleanup'`

**Step 3: Write the implementation**

Create `src/scrapeyard/storage/cleanup.py`:

```python
"""Periodic cleanup of expired scrape results."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scrapeyard.storage.result_store import LocalResultStore

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL = 3600  # 1 hour


def start_cleanup_loop(
    result_store: LocalResultStore,
    retention_days: int,
    interval_seconds: float = _DEFAULT_INTERVAL,
) -> asyncio.Task:
    """Spawn a background task that periodically deletes expired results.

    Returns the :class:`asyncio.Task` so the caller can cancel it on shutdown.
    """

    async def _loop() -> None:
        while True:
            try:
                deleted = await result_store.delete_expired(retention_days)
                if deleted:
                    logger.info("Cleanup removed %d expired result(s)", deleted)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error during result cleanup")
            await asyncio.sleep(interval_seconds)

    return asyncio.create_task(_loop(), name="result-cleanup")
```

**Step 4: Run test to verify it passes**

Run: `source .venv/bin/activate && pytest tests/unit/test_cleanup_loop.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/scrapeyard/storage/cleanup.py tests/unit/test_cleanup_loop.py
git commit -m "feat: add start_cleanup_loop for result retention"
```

---

### Task 4: Wire everything into the lifespan

**Files:**
- Modify: `src/scrapeyard/main.py`

**Step 1: Rewrite the lifespan function**

Replace the entire content of `src/scrapeyard/main.py` with:

```python
"""FastAPI application entry point."""

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI

from scrapeyard.api.dependencies import (
    get_error_store,
    get_job_store,
    get_result_store,
    get_scheduler,
    get_worker_pool,
)
from scrapeyard.api.routes import router
from scrapeyard.common.logging import setup_logging
from scrapeyard.common.settings import get_settings
from scrapeyard.storage.cleanup import start_cleanup_loop
from scrapeyard.storage.database import init_db, reset_db

_start_time: float = 0.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Orchestrate startup and shutdown in dependency order."""
    global _start_time  # noqa: PLW0603
    _start_time = time.monotonic()

    # 1. Settings & logging
    settings = get_settings()
    setup_logging(settings.log_dir)

    # 2. Database
    await init_db(settings.db_dir)

    # 3. Storage instances
    app.state.job_store = get_job_store()
    app.state.error_store = get_error_store()
    app.state.result_store = get_result_store()

    # 4. Worker pool
    pool = get_worker_pool()
    app.state.worker_pool = pool
    await pool.start()

    # 5. Scheduler (re-registers persisted jobs on start)
    scheduler = get_scheduler()
    app.state.scheduler = scheduler
    await scheduler.start()

    # 6. Cleanup loop
    app.state.cleanup_task = start_cleanup_loop(
        app.state.result_store,
        retention_days=settings.storage_retention_days,
    )

    yield

    # Shutdown (reverse order)
    app.state.cleanup_task.cancel()
    try:
        await app.state.cleanup_task
    except asyncio.CancelledError:
        pass
    scheduler.shutdown()
    await pool.stop()
    reset_db()


app = FastAPI(
    title="Scrapeyard",
    description="Config-driven web scraping microservice",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(router)


@app.get("/health")
async def health() -> dict:
    """Service health check endpoint with detailed status."""
    pool = get_worker_pool()

    uptime = time.monotonic() - _start_time if _start_time else 0.0

    # Determine overall status based on active task load.
    status = "ok"
    if pool.active_tasks >= pool._max_concurrent:
        status = "degraded"

    return {
        "status": status,
        "uptime_seconds": round(uptime, 1),
        "workers": {
            "max_concurrent": pool._max_concurrent,
            "active_tasks": pool.active_tasks,
            "max_browsers": pool._max_browsers,
            "active_browsers": 0,
        },
    }
```

Note: Must also add `import asyncio` at the top.

**Step 2: Run the health test to verify it still passes**

Run: `source .venv/bin/activate && pytest tests/unit/test_health.py -v`
Expected: PASS — but it may fail because `init_db` needs a real directory. See Task 5.

**Step 3: Commit (after Task 5 if tests need fixing)**

```bash
git add src/scrapeyard/main.py
git commit -m "feat: wire complete lifespan with init_db, storage, and cleanup loop"
```

---

### Task 5: Fix test fixtures for `init_db` in test_health

**Files:**
- Create: `tests/conftest.py`
- Modify: `tests/unit/test_health.py` (if needed)

**Step 1: Create a shared conftest that sets temp dirs**

Create `tests/conftest.py`:

```python
"""Root test configuration — sets safe temp directories for all tests."""

import os

import pytest


@pytest.fixture(autouse=True)
def _scrapeyard_temp_dirs(tmp_path, monkeypatch):
    """Point all data directories to temp paths for every test."""
    monkeypatch.setenv("SCRAPEYARD_DB_DIR", str(tmp_path / "db"))
    monkeypatch.setenv("SCRAPEYARD_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("SCRAPEYARD_STORAGE_RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("SCRAPEYARD_ADAPTIVE_DIR", str(tmp_path / "adaptive"))

    # Clear cached singletons so they pick up the test env vars.
    from scrapeyard.common.settings import get_settings
    get_settings.cache_clear()

    yield

    get_settings.cache_clear()
```

Note: The `lru_cache` on `get_settings` needs clearing so each test gets fresh settings pointing at temp dirs. The dependency singletons (`get_job_store`, etc.) also use `lru_cache` — they need clearing too.

Update `tests/conftest.py` to also clear dependency caches:

```python
"""Root test configuration — sets safe temp directories for all tests."""

import pytest


@pytest.fixture(autouse=True)
def _scrapeyard_temp_dirs(tmp_path, monkeypatch):
    """Point all data directories to temp paths for every test."""
    monkeypatch.setenv("SCRAPEYARD_DB_DIR", str(tmp_path / "db"))
    monkeypatch.setenv("SCRAPEYARD_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("SCRAPEYARD_STORAGE_RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("SCRAPEYARD_ADAPTIVE_DIR", str(tmp_path / "adaptive"))

    from scrapeyard.common.settings import get_settings
    from scrapeyard.api.dependencies import (
        get_circuit_breaker,
        get_error_store,
        get_job_store,
        get_result_store,
        get_scheduler,
        get_worker_pool,
    )

    # Clear all cached singletons.
    for cached_fn in [
        get_settings,
        get_job_store,
        get_error_store,
        get_result_store,
        get_circuit_breaker,
        get_worker_pool,
        get_scheduler,
    ]:
        cached_fn.cache_clear()

    yield

    for cached_fn in [
        get_settings,
        get_job_store,
        get_error_store,
        get_result_store,
        get_circuit_breaker,
        get_worker_pool,
        get_scheduler,
    ]:
        cached_fn.cache_clear()
```

**Step 2: Run the health tests**

Run: `source .venv/bin/activate && pytest tests/unit/test_health.py -v`
Expected: PASS

**Step 3: Run all tests**

Run: `source .venv/bin/activate && pytest tests/ -v`
Expected: All PASS

**Step 4: Commit**

```bash
git add tests/conftest.py
git commit -m "test: add root conftest with temp dir fixtures for lifespan tests"
```

---

### Task 6: Run linter and final verification

**Files:** None (verification only)

**Step 1: Run ruff**

Run: `source .venv/bin/activate && ruff check src/scrapeyard/main.py src/scrapeyard/storage/cleanup.py src/scrapeyard/storage/database.py src/scrapeyard/storage/result_store.py src/scrapeyard/storage/protocols.py`
Expected: No errors (fix any that appear)

**Step 2: Run full test suite**

Run: `source .venv/bin/activate && pytest tests/ -v`
Expected: All PASS

**Step 3: Verify app imports correctly**

Run: `source .venv/bin/activate && python -c "from scrapeyard.main import app; print(type(app))"`
Expected: `<class 'fastapi.applications.FastAPI'>`

**Step 4: Final commit if any lint fixes were needed**

```bash
git add -u
git commit -m "chore: lint fixes for lifespan wiring"
```
