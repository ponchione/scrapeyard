"""Database initialization and connection management for SQLite stores."""

from __future__ import annotations

import importlib.resources
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

# Mapping of database filename to its initial migration script.
_DB_MIGRATIONS: dict[str, str] = {
    "jobs.db": "001_create_jobs.sql",
    "errors.db": "002_create_errors.sql",
    "results_meta.db": "003_create_results_meta.sql",
}

# Module-level cache of database directory after init.
_db_dir: Path | None = None


def reset_db() -> None:
    """Clear the module-level database directory, signalling teardown."""
    global _db_dir  # noqa: PLW0603
    _db_dir = None


async def close_db() -> None:
    """Close database subsystem state.

    Connections are opened per-operation via :func:`get_db`; this function
    exists to provide an explicit shutdown hook for app lifespan teardown.
    """
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
    db_path.mkdir(parents=True, exist_ok=True)
    _db_dir = db_path

    sql_dir = importlib.resources.files("scrapeyard") / "../../sql"
    # Resolve to an actual filesystem path so we can read the files.
    sql_dir = Path(str(sql_dir)).resolve()

    for db_name, migration_file in _DB_MIGRATIONS.items():
        migration_sql = (sql_dir / migration_file).read_text()
        async with aiosqlite.connect(db_path / db_name) as db:
            await db.executescript(migration_sql)
            if db_name == "errors.db":
                await _ensure_error_columns(db)
            await db.commit()


async def _ensure_error_columns(db: aiosqlite.Connection) -> None:
    """Backfill newer error-table columns for existing databases."""
    cursor = await db.execute("PRAGMA table_info(errors)")
    rows = await cursor.fetchall()
    columns = {row[1] for row in rows}
    if "error_message" not in columns:
        await db.execute("ALTER TABLE errors ADD COLUMN error_message TEXT")


@asynccontextmanager
async def get_db(db_name: str) -> AsyncIterator[aiosqlite.Connection]:
    """Yield an :class:`aiosqlite.Connection` to the named database.

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
    async with aiosqlite.connect(_db_dir / db_name) as db:
        yield db
