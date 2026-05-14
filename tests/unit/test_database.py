"""Tests for database initialization and connection management."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from scrapeyard.storage.database import close_db, get_db, init_db


async def test_init_db_creates_databases(tmp_path):
    """init_db should create all three .db files."""
    db_dir = tmp_path / "db"
    await init_db(str(db_dir))

    assert (db_dir / "jobs.db").exists()
    assert (db_dir / "errors.db").exists()
    assert (db_dir / "results_meta.db").exists()


async def test_init_db_creates_tables(tmp_path):
    """Tables should exist after init_db runs."""
    db_dir = tmp_path / "db"
    await init_db(str(db_dir))

    async with get_db("jobs.db") as db:
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='jobs'"
        )
        row = await cursor.fetchone()
        assert row is not None
        cursor = await db.execute("PRAGMA table_info(jobs)")
        columns = {column[1] for column in await cursor.fetchall()}
        assert "schedule_enabled" in columns
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='webhook_deliveries'"
        )
        row = await cursor.fetchone()
        assert row is not None

    async with get_db("errors.db") as db:
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='errors'"
        )
        row = await cursor.fetchone()
        assert row is not None

    async with get_db("results_meta.db") as db:
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='results_meta'"
        )
        row = await cursor.fetchone()
        assert row is not None


async def test_init_db_idempotent(tmp_path):
    """Calling init_db twice should not raise."""
    db_dir = tmp_path / "db"
    await init_db(str(db_dir))
    await init_db(str(db_dir))


async def test_init_db_deduplicates_result_meta_before_unique_index(tmp_path):
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    sql_dir = Path(__file__).resolve().parents[2] / "sql"
    async with aiosqlite.connect(db_dir / "results_meta.db") as db:
        await db.executescript((sql_dir / "003_create_results_meta.sql").read_text())
        await db.executescript((sql_dir / "006_add_results_meta_indexes.sql").read_text())
        await db.executemany(
            """INSERT INTO results_meta
               (job_id, project, run_id, status, record_count, file_path, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    "job-1",
                    "acme",
                    "run-1",
                    "partial",
                    1,
                    "/tmp/old",
                    "2026-01-01T00:00:00+00:00",
                ),
                (
                    "job-1",
                    "acme",
                    "run-1",
                    "complete",
                    2,
                    "/tmp/new",
                    "2026-01-01T00:01:00+00:00",
                ),
            ],
        )
        await db.commit()

    await init_db(str(db_dir))

    async with get_db("results_meta.db") as db:
        cursor = await db.execute(
            "SELECT status, record_count, file_path FROM results_meta WHERE job_id=? AND run_id=?",
            ("job-1", "run-1"),
        )
        rows = await cursor.fetchall()
        cursor = await db.execute(
            "SELECT name, [unique] FROM pragma_index_list('results_meta') "
            "WHERE name='idx_results_meta_job_run'"
        )
        index_row = await cursor.fetchone()

    assert [(row["status"], row["record_count"], row["file_path"]) for row in rows] == [
        ("complete", 2, "/tmp/new")
    ]
    assert index_row is not None
    assert index_row["unique"] == 1


async def test_get_db_before_init():
    """get_db should raise RuntimeError if init_db was not called."""
    # Reset module state to simulate no init.
    import scrapeyard.storage.database as mod

    original = mod._default_manager._db_dir
    mod._default_manager._db_dir = None
    try:
        with pytest.raises(RuntimeError, match="not initialised"):
            async with get_db("jobs.db"):
                pass
    finally:
        mod._default_manager._db_dir = original


async def test_get_db_unknown_name(tmp_path):
    """get_db should raise ValueError for an unknown db name."""
    await init_db(str(tmp_path / "db"))
    with pytest.raises(ValueError, match="Unknown database"):
        async with get_db("nope.db"):
            pass


async def test_get_db_reuses_cached_connection(tmp_path):
    """Repeated access to the same DB should reuse the cached connection."""
    await init_db(str(tmp_path / "db"))

    async with get_db("jobs.db") as first:
        pass

    async with get_db("jobs.db") as second:
        pass

    assert first is second


async def test_init_db_switches_cached_connections_for_new_path(tmp_path):
    """Reinitializing to a new DB dir should not reuse the old connection."""
    await init_db(str(tmp_path / "db-1"))
    async with get_db("jobs.db") as first:
        pass

    await init_db(str(tmp_path / "db-2"))
    async with get_db("jobs.db") as second:
        pass

    assert first is not second
    await close_db()
