"""Database initialization and connection management for SQLite stores."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.resources
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re

import aiosqlite

# Mapping of database filename to its forward-only migration history.
_DB_MIGRATIONS: dict[str, tuple[str, ...]] = {
    "jobs.db": (
        "001_create_jobs.sql",
        "004_create_job_runs.sql",
        "005_add_indexes.sql",
        "009_create_webhook_outbox.sql",
        "010_add_terminal_reconciliation_marker.sql",
        "012_create_scrape_idempotency.sql",
        "013_add_schedule_timezone.sql",
        "014_add_jobs_config_hash.sql",
        "015_add_jobs_current_trigger.sql",
        "016_add_webhook_decode_failure_reason.sql",
        "017_add_history_retention_summary.sql",
        "018_add_run_snapshots_and_schedule_health.sql",
        "019_create_queued_run_snapshots.sql",
    ),
    "errors.db": ("002_create_errors.sql", "007_add_errors_indexes.sql"),
    "results_meta.db": (
        "003_create_results_meta.sql",
        "006_add_results_meta_indexes.sql",
        "008_results_meta_unique_job_run.sql",
        "011_add_results_artifact_lookup_index.sql",
        "020_create_result_reconciliation_state.sql",
    ),
}
_MIGRATION_FILE_RE = re.compile(r"^(?P<id>[0-9]{3})_[a-z0-9_]+\.sql$")
_MIGRATION_TABLE = "schema_migrations"

_CONNECTION_PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA busy_timeout = 5000",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA foreign_keys = ON",
)


@dataclass(frozen=True, slots=True)
class Migration:
    migration_id: str
    filename: str
    sql: str
    checksum: str


def _load_migrations(sql_dir: Path) -> dict[str, tuple[Migration, ...]]:
    """Load and validate the complete, explicitly assigned migration history."""

    discovered: dict[str, str] = {}
    for path in sorted(sql_dir.glob("*.sql")):
        match = _MIGRATION_FILE_RE.fullmatch(path.name)
        if match is None:
            raise RuntimeError(f"Invalid migration filename: {path.name!r}")
        migration_id = match.group("id")
        existing = discovered.get(migration_id)
        if existing is not None:
            raise RuntimeError(
                f"Duplicate migration ID {migration_id}: {existing!r}, {path.name!r}"
            )
        discovered[migration_id] = path.name

    assigned = [filename for files in _DB_MIGRATIONS.values() for filename in files]
    if len(assigned) != len(set(assigned)):
        raise RuntimeError("A migration file is assigned to more than one database")
    assigned_by_id: dict[str, str] = {}
    for filename in assigned:
        match = _MIGRATION_FILE_RE.fullmatch(filename)
        if match is None:
            raise RuntimeError(f"Invalid assigned migration filename: {filename!r}")
        migration_id = match.group("id")
        if migration_id in assigned_by_id:
            raise RuntimeError(f"Migration ID {migration_id} is assigned more than once")
        assigned_by_id[migration_id] = filename

    if discovered != assigned_by_id:
        missing = sorted(set(assigned_by_id.values()) - set(discovered.values()))
        unassigned = sorted(set(discovered.values()) - set(assigned_by_id.values()))
        raise RuntimeError(
            "Migration assignment mismatch: "
            f"missing={missing or 'none'} unassigned={unassigned or 'none'}"
        )

    numeric_ids = sorted(int(value) for value in discovered)
    if numeric_ids != list(range(1, numeric_ids[-1] + 1)):
        raise RuntimeError(f"Migration history contains a numeric gap: {numeric_ids}")

    loaded: dict[str, tuple[Migration, ...]] = {}
    for db_name, filenames in _DB_MIGRATIONS.items():
        migrations: list[Migration] = []
        for filename in filenames:
            sql = (sql_dir / filename).read_text(encoding="utf-8")
            migration_id = filename.split("_", 1)[0]
            migrations.append(
                Migration(
                    migration_id=migration_id,
                    filename=filename,
                    sql=sql,
                    checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
                )
            )
        loaded[db_name] = tuple(migrations)
    return loaded


async def _ensure_migration_table(db: aiosqlite.Connection) -> None:
    await db.execute(
        f"""CREATE TABLE IF NOT EXISTS {_MIGRATION_TABLE} (
               migration_id TEXT PRIMARY KEY,
               filename TEXT NOT NULL UNIQUE,
               checksum TEXT NOT NULL,
               applied_at TEXT NOT NULL
           )"""
    )
    await db.commit()


async def _table_exists(db: aiosqlite.Connection, name: str) -> bool:
    cursor = await db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    )
    return await cursor.fetchone() is not None


async def _index_exists(
    db: aiosqlite.Connection,
    name: str,
    *,
    unique: bool | None = None,
) -> bool:
    cursor = await db.execute(
        "SELECT 1, sql FROM sqlite_master WHERE type = 'index' AND name = ?",
        (name,),
    )
    row = await cursor.fetchone()
    if row is None:
        return False
    if unique is None:
        return True
    sql = str(row[1] or "").upper()
    return ("CREATE UNIQUE INDEX" in sql) is unique


async def _columns_include(
    db: aiosqlite.Connection,
    table: str,
    required: set[str],
) -> bool:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    columns = {str(row[1]) for row in await cursor.fetchall()}
    return required <= columns


async def _migration_is_reflected(
    db: aiosqlite.Connection,
    migration_id: str,
) -> bool:
    """Recognize a completed pre-ledger migration without replaying it."""

    if migration_id == "001":
        return await _columns_include(
            db,
            "jobs",
            {
                "job_id",
                "project",
                "name",
                "status",
                "config_yaml",
                "created_at",
                "updated_at",
                "schedule_cron",
                "schedule_enabled",
                "current_run_id",
                "deletion_requested_at",
                "delete_results_on_delete",
            },
        )
    if migration_id == "002":
        return await _table_exists(db, "errors")
    if migration_id == "003":
        return await _table_exists(db, "results_meta")
    if migration_id == "004":
        return await _columns_include(db, "job_runs", {"heartbeat_at"})
    if migration_id == "005":
        return await _index_exists(db, "idx_jobs_project") and await _index_exists(
            db, "idx_job_runs_job_started"
        )
    if migration_id == "006":
        return await _index_exists(db, "idx_results_meta_job_created") and await _index_exists(
            db, "idx_results_meta_job_run"
        )
    if migration_id == "007":
        return await _index_exists(db, "idx_errors_job_timestamp") and await _index_exists(
            db, "idx_errors_project_timestamp"
        )
    if migration_id == "008":
        return await _index_exists(db, "idx_results_meta_job_run", unique=True)
    if migration_id == "009":
        return await _columns_include(
            db,
            "webhook_deliveries",
            {"failed_at", "failure_reason", "scrubbed_at"},
        )
    if migration_id == "010":
        return await _columns_include(db, "job_runs", {"webhook_reconciled_at"})
    if migration_id == "011":
        return await _index_exists(db, "idx_results_meta_project_run")
    if migration_id == "012":
        return await _columns_include(
            db,
            "scrape_idempotency",
            {
                "caller_scope",
                "key_digest",
                "request_hash",
                "job_id",
                "run_id",
                "response_mode",
                "created_at",
                "expires_at",
            },
        ) and await _index_exists(db, "idx_scrape_idempotency_expires")
    if migration_id == "013":
        return await _columns_include(db, "jobs", {"schedule_timezone"})
    if migration_id == "014":
        return await _columns_include(db, "jobs", {"config_hash"})
    if migration_id == "015":
        return await _columns_include(db, "jobs", {"current_trigger"})
    if migration_id == "016":
        return False
    if migration_id == "017":
        return False
    if migration_id == "018":
        return False
    if migration_id == "020":
        return await _table_exists(db, "result_reconciliation_state")
    return False


async def _prepare_legacy_schema(
    db_name: str,
    db: aiosqlite.Connection,
) -> None:
    """Bring known pre-ledger compatibility columns to their final old shape."""

    if db_name != "jobs.db":
        return
    if await _table_exists(db, "job_runs"):
        await _ensure_job_runs_heartbeat_column(db)
    if await _table_exists(db, "webhook_deliveries"):
        await _ensure_webhook_outbox_item06_columns(db)
    if await _table_exists(db, "jobs"):
        await _ensure_jobs_item07_columns(db)
    await db.commit()


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


async def _apply_migration(
    db: aiosqlite.Connection,
    migration: Migration,
) -> None:
    applied_at = datetime.now(timezone.utc).isoformat()
    ledger_insert = (
        f"INSERT INTO {_MIGRATION_TABLE} "
        "(migration_id, filename, checksum, applied_at) VALUES ("
        f"{_sql_literal(migration.migration_id)}, "
        f"{_sql_literal(migration.filename)}, "
        f"{_sql_literal(migration.checksum)}, "
        f"{_sql_literal(applied_at)});"
    )
    script = f"BEGIN IMMEDIATE;\n{migration.sql}\n{ledger_insert}\nCOMMIT;"
    try:
        await db.executescript(script)
    except BaseException:
        if db.in_transaction:
            await db.rollback()
        raise


async def _record_baseline(
    db: aiosqlite.Connection,
    migration: Migration,
) -> None:
    await db.execute("BEGIN IMMEDIATE")
    try:
        await db.execute(
            f"""INSERT INTO {_MIGRATION_TABLE}
                (migration_id, filename, checksum, applied_at)
                VALUES (?, ?, ?, ?)""",
            (
                migration.migration_id,
                migration.filename,
                migration.checksum,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await db.commit()
    except BaseException:
        if db.in_transaction:
            await db.rollback()
        raise


async def _migrate_database(
    db_name: str,
    db: aiosqlite.Connection,
    migrations: tuple[Migration, ...],
) -> None:
    had_ledger = await _table_exists(db, _MIGRATION_TABLE)
    if not had_ledger:
        await _prepare_legacy_schema(db_name, db)
    await _ensure_migration_table(db)

    cursor = await db.execute(
        f"SELECT migration_id, filename, checksum FROM {_MIGRATION_TABLE} ORDER BY migration_id"
    )
    rows = await cursor.fetchall()
    expected_by_id = {migration.migration_id: migration for migration in migrations}
    recorded_ids: list[str] = []
    for row in rows:
        migration_id = str(row[0])
        migration = expected_by_id.get(migration_id)
        if migration is None:
            raise RuntimeError(
                f"Migration {migration_id} is recorded in the wrong database {db_name}"
            )
        if str(row[1]) != migration.filename:
            raise RuntimeError(
                f"Migration filename drift for {migration_id}: "
                f"recorded={row[1]!r} expected={migration.filename!r}"
            )
        if str(row[2]) != migration.checksum:
            raise RuntimeError(f"Migration checksum drift for {migration.filename}")
        recorded_ids.append(migration_id)

    expected_ids = [migration.migration_id for migration in migrations]
    if recorded_ids != expected_ids[: len(recorded_ids)]:
        raise RuntimeError(
            f"Migration ledger gap in {db_name}: recorded={recorded_ids} "
            f"expected_prefix={expected_ids[: len(recorded_ids)]}"
        )

    for migration in migrations[len(recorded_ids) :]:
        if not had_ledger and await _migration_is_reflected(db, migration.migration_id):
            await _record_baseline(db, migration)
        else:
            await _apply_migration(db, migration)


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


@asynccontextmanager
async def db_transaction(
    db: aiosqlite.Connection,
    *,
    immediate: bool = False,
) -> AsyncIterator[None]:
    """Commit one explicit transaction or roll it back on any cancellation/fault.

    Callers may deliberately roll back a compare-and-set no-op before leaving the
    context. In that case there is no active transaction left to commit.
    """

    await db.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        if db.in_transaction:
            await db.rollback()
        raise
    else:
        if db.in_transaction:
            try:
                await db.commit()
            except BaseException:
                if db.in_transaction:
                    await db.rollback()
                raise


async def _ensure_job_runs_heartbeat_column(db: aiosqlite.Connection) -> None:
    """Add the lease column when baselining a known pre-ledger database.

    SQLite has no portable ``ADD COLUMN IF NOT EXISTS`` form, so legacy
    compatibility upgrades use this narrow schema check before the migration
    history is recorded.
    """
    cursor = await db.execute("PRAGMA table_info(job_runs)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "heartbeat_at" not in columns:
        await db.execute("ALTER TABLE job_runs ADD COLUMN heartbeat_at TEXT")
        await db.execute("UPDATE job_runs SET heartbeat_at = started_at WHERE heartbeat_at IS NULL")


async def _ensure_webhook_outbox_item06_columns(db: aiosqlite.Connection) -> None:
    """Apply the narrow outbox upgrade needed before legacy baselining."""

    cursor = await db.execute("PRAGMA table_info(webhook_deliveries)")
    columns = {row[1] for row in await cursor.fetchall()}
    additions = {
        "failed_at": "TEXT",
        "failure_reason": "TEXT",
        "scrubbed_at": "TEXT",
    }
    for column, declaration in additions.items():
        if column not in columns:
            await db.execute(f"ALTER TABLE webhook_deliveries ADD COLUMN {column} {declaration}")

    await db.execute(
        """UPDATE webhook_deliveries
           SET failed_at = COALESCE(failed_at, last_attempt_at, updated_at),
               failure_reason = COALESCE(failure_reason, 'non_retryable_failure')
           WHERE status = 'failed'
             AND (failed_at IS NULL OR failure_reason IS NULL)"""
    )
    await db.execute(
        """CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_terminal_cleanup
           ON webhook_deliveries (status, scrubbed_at, delivered_at, failed_at)"""
    )


async def _ensure_jobs_item07_columns(db: aiosqlite.Connection) -> None:
    """Apply cancellation/deletion columns needed before legacy baselining."""

    cursor = await db.execute("PRAGMA table_info(jobs)")
    columns = {row[1] for row in await cursor.fetchall()}
    additions = {
        "deletion_requested_at": "TEXT",
        "delete_results_on_delete": "INTEGER",
    }
    for column, declaration in additions.items():
        if column not in columns:
            await db.execute(f"ALTER TABLE jobs ADD COLUMN {column} {declaration}")
    cursor = await db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'webhook_deliveries'"
    )
    if await cursor.fetchone() is not None:
        await db.execute(
            """CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_job_status
               ON webhook_deliveries (job_id, status)"""
        )


class DatabaseManager:
    """Encapsulates database directory, cached connections, and re-entrant locks.

    Replaces the former module-level globals (_db_dir, _db_connections, etc.)
    with instance state, making it easier to test and reason about lifecycle.
    """

    def __init__(self) -> None:
        self._db_dir: Path | None = None
        self._connections: dict[str, aiosqlite.Connection] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def _close_cached_connections(self) -> None:
        connections = list(self._connections.values())
        self._connections.clear()
        for conn in connections:
            await conn.close()

    async def close(self) -> None:
        """Close cached SQLite connections and clear state."""
        await self._close_cached_connections()
        self._db_dir = None
        self._locks.clear()

    async def init(self, db_dir: str) -> None:
        """Create *db_dir* (if needed), open each database, and apply migrations."""
        db_path = Path(db_dir)
        if self._connections and self._db_dir != db_path:
            await self.close()
        db_path.mkdir(parents=True, exist_ok=True)
        self._db_dir = db_path

        sql_dir = _resolve_sql_dir()
        migrations = _load_migrations(sql_dir)

        for db_name in _DB_MIGRATIONS:
            async with aiosqlite.connect(db_path / db_name) as db:
                await _apply_connection_pragmas(db)
                await _migrate_database(db_name, db, migrations[db_name])

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

    async def probe(self, db_name: str) -> None:
        """Open the real database read/write and verify its on-disk structure."""

        if self._db_dir is None:
            raise RuntimeError("Database not initialised — call init_db() first")
        if db_name not in _DB_MIGRATIONS:
            raise ValueError(f"Unknown database: {db_name!r}")
        path = self._db_dir / db_name
        descriptor = os.open(path, os.O_RDWR)
        os.close(descriptor)
        async with aiosqlite.connect(f"file:{path}?mode=rw", uri=True) as db:
            cursor = await db.execute("PRAGMA quick_check(1)")
            row = await cursor.fetchone()
            if row is None or row[0] != "ok":
                raise RuntimeError(f"SQLite quick_check failed for {db_name}")

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


async def probe_db(db_name: str) -> None:
    """Verify a fresh read/write open and structural check of one database."""

    await _default_manager.probe(db_name)
