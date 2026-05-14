"""Database initialization and connection management for SQLite stores."""

from __future__ import annotations

import asyncio
import importlib.resources
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite

# Mapping of database filename to its migration scripts (executed in order).
_DB_MIGRATIONS: dict[str, list[str]] = {
    "jobs.db": [
        "001_create_jobs.sql",
        "004_create_job_runs.sql",
        "005_add_indexes.sql",
        "009_create_webhook_outbox.sql",
    ],
    "errors.db": ["002_create_errors.sql", "007_add_errors_indexes.sql"],
    "results_meta.db": ["003_create_results_meta.sql", "006_add_results_meta_indexes.sql", "008_results_meta_unique_job_run.sql"],
}

_CONNECTION_PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA busy_timeout = 5000",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA foreign_keys = ON",
)


def _resolve_sql_dir() -> Path:
    """Return the SQL migration directory for source and wheel installs."""
    package_dir = Path(str(importlib.resources.files("scrapeyard"))).resolve()
    candidates = (
        package_dir.parent / "sql",
        package_dir.parent.parent / "sql",
    )
    for candidate in candidates:
        if (candidate / "001_create_jobs.sql").is_file():
            return candidate
    raise RuntimeError("Could not find packaged SQL migrations")


async def _apply_connection_pragmas(db: aiosqlite.Connection) -> None:
    for pragma in _CONNECTION_PRAGMAS:
        await db.execute(pragma)


class DatabaseManager:
    """Encapsulates database directory, cached connections, and re-entrant locks.

    Replaces the former module-level globals (_db_dir, _db_connections, etc.)
    with instance state, making it easier to test and reason about lifecycle.
    """

    def __init__(self) -> None:
        self._db_dir: Path | None = None
        self._connections: dict[str, aiosqlite.Connection] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def reset(self) -> None:
        """Clear state, signalling teardown."""
        self._db_dir = None
        self._locks.clear()

    async def _close_cached_connections(self) -> None:
        connections = list(self._connections.values())
        self._connections.clear()
        for conn in connections:
            await conn.close()

    async def close(self) -> None:
        """Close cached SQLite connections and clear state."""
        await self._close_cached_connections()
        self.reset()

    async def init(self, db_dir: str) -> None:
        """Create *db_dir* (if needed), open each database, and apply migrations."""
        db_path = Path(db_dir)
        if self._connections and self._db_dir != db_path:
            await self._close_cached_connections()
            self.reset()
        db_path.mkdir(parents=True, exist_ok=True)
        self._db_dir = db_path

        sql_dir = _resolve_sql_dir()

        for db_name, migration_files in _DB_MIGRATIONS.items():
            async with aiosqlite.connect(db_path / db_name) as db:
                await _apply_connection_pragmas(db)
                for migration_file in migration_files:
                    migration_sql = (sql_dir / migration_file).read_text()
                    await db.executescript(migration_sql)
                await db.commit()

    async def _get_cached_connection(self, db_name: str) -> aiosqlite.Connection:
        connection = self._connections.get(db_name)
        if connection is None:
            if self._db_dir is None:
                raise RuntimeError("Database not initialised — call init_db() first")
            connection = await aiosqlite.connect(self._db_dir / db_name)
            connection.row_factory = aiosqlite.Row
            await _apply_connection_pragmas(connection)
            self._connections[db_name] = connection
        return connection

    @asynccontextmanager
    async def get(self, db_name: str) -> AsyncIterator[aiosqlite.Connection]:
        """Yield a cached connection to the named database.

        Raises RuntimeError if init() has not been called, ValueError if
        *db_name* is unrecognised.
        """
        if self._db_dir is None:
            raise RuntimeError("Database not initialised — call init_db() first")
        if db_name not in _DB_MIGRATIONS:
            raise ValueError(f"Unknown database: {db_name!r}")

        async with self._locks.setdefault(db_name, asyncio.Lock()):
            yield await self._get_cached_connection(db_name)


_default_manager = DatabaseManager()


def reset_db() -> None:
    """Clear module-level database state, signalling teardown."""
    _default_manager.reset()


async def close_db() -> None:
    """Close cached SQLite connections and clear module-level state."""
    await _default_manager.close()


async def init_db(db_dir: str) -> None:
    """Create *db_dir* (if needed), open each database, and apply migrations.

    Parameters
    ----------
    db_dir:
        Filesystem path where ``*.db`` files are stored.
    """
    await _default_manager.init(db_dir)


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
    async with _default_manager.get(db_name) as conn:
        yield conn
