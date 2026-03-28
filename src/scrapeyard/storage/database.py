"""Database initialization and connection management for SQLite stores."""

from __future__ import annotations

import asyncio
import importlib.resources
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

# Mapping of database filename to its migration scripts (executed in order).
_DB_MIGRATIONS: dict[str, list[str]] = {
    "jobs.db": ["001_create_jobs.sql", "004_create_job_runs.sql"],
    "errors.db": ["002_create_errors.sql"],
    "results_meta.db": ["003_create_results_meta.sql"],
}

# Module-level cache of database directory after init.
_db_dir: Path | None = None
_db_connections: dict[str, aiosqlite.Connection] = {}
_db_locks: dict[str, asyncio.Lock] = {}
_db_lock_owners: dict[str, asyncio.Task[object] | None] = {}
_db_lock_depths: dict[str, int] = {}


def reset_db() -> None:
    """Clear module-level database state, signalling teardown."""
    global _db_dir  # noqa: PLW0603
    _db_dir = None
    _db_locks.clear()
    _db_lock_owners.clear()
    _db_lock_depths.clear()


async def _close_cached_connections() -> None:
    connections = list(_db_connections.values())
    _db_connections.clear()
    for conn in connections:
        await conn.close()


async def close_db() -> None:
    """Close cached SQLite connections and clear module-level state."""
    await _close_cached_connections()
    reset_db()


async def init_db(db_dir: str) -> None:
    """Create *db_dir* (if needed), open each database, and apply migrations.

    Parameters
    ----------
    db_dir:
        Filesystem path where ``*.db`` files are stored.
    """
    global _db_dir  # noqa: PLW0603
    db_path = Path(db_dir)
    if _db_connections and _db_dir != db_path:
        await _close_cached_connections()
        reset_db()
    db_path.mkdir(parents=True, exist_ok=True)
    _db_dir = db_path

    sql_dir = importlib.resources.files("scrapeyard") / "../../sql"
    # Resolve to an actual filesystem path so we can read the files.
    sql_dir = Path(str(sql_dir)).resolve()

    for db_name, migration_files in _DB_MIGRATIONS.items():
        async with aiosqlite.connect(db_path / db_name) as db:
            for migration_file in migration_files:
                migration_sql = (sql_dir / migration_file).read_text()
                await db.executescript(migration_sql)
            await db.commit()


async def _get_cached_connection(db_name: str) -> aiosqlite.Connection:
    connection = _db_connections.get(db_name)
    if connection is None:
        if _db_dir is None:
            raise RuntimeError("Database not initialised — call init_db() first")
        connection = await aiosqlite.connect(_db_dir / db_name)
        _db_connections[db_name] = connection
    return connection


@asynccontextmanager
async def get_db(db_name: str) -> AsyncIterator[aiosqlite.Connection]:
    """Yield a cached :class:`aiosqlite.Connection` to the named database.

    Parameters
    ----------
    db_name:
        One of ``jobs.db``, ``errors.db``, or ``results_meta.db``.

    Raises
    ------
    RuntimeError
        If :func:`init_db` has not been called yet.
    ValueError
        If *db_name* is not a recognised database.
    """
    if _db_dir is None:
        raise RuntimeError("Database not initialised — call init_db() first")
    if db_name not in _DB_MIGRATIONS:
        raise ValueError(f"Unknown database: {db_name!r}")

    lock = _db_locks.setdefault(db_name, asyncio.Lock())
    owner = asyncio.current_task()
    acquired_here = _db_lock_owners.get(db_name) is not owner
    if acquired_here:
        await lock.acquire()
        _db_lock_owners[db_name] = owner
        _db_lock_depths[db_name] = 0

    _db_lock_depths[db_name] = _db_lock_depths.get(db_name, 0) + 1
    try:
        yield await _get_cached_connection(db_name)
    finally:
        depth = _db_lock_depths[db_name] - 1
        if depth == 0:
            _db_lock_depths.pop(db_name, None)
            _db_lock_owners.pop(db_name, None)
            if acquired_here:
                lock.release()
        else:
            _db_lock_depths[db_name] = depth
