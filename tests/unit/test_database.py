"""Tests for database initialization and connection management."""

from __future__ import annotations

import pytest

from scrapeyard.storage.database import get_db, init_db


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


async def test_get_db_before_init():
    """get_db should raise RuntimeError if init_db was not called."""
    # Reset module state to simulate no init.
    import scrapeyard.storage.database as mod

    original = mod._db_dir
    mod._db_dir = None
    try:
        with pytest.raises(RuntimeError, match="not initialised"):
            async with get_db("jobs.db"):
                pass
    finally:
        mod._db_dir = original


async def test_get_db_unknown_name(tmp_path):
    """get_db should raise ValueError for an unknown db name."""
    await init_db(str(tmp_path / "db"))
    with pytest.raises(ValueError, match="Unknown database"):
        async with get_db("nope.db"):
            pass
