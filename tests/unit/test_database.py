"""Tests for database initialization and connection management."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
import pytest

from scrapeyard.storage.database import (
    Migration,
    _apply_migration,
    _load_migrations,
    _resolve_sql_dir,
    close_db,
    db_transaction,
    get_db,
    init_db,
)
from scrapeyard.storage.job_store import SQLiteJobStore
from scrapeyard.storage.secret_envelope import migrate_persisted_secrets


def test_resolve_sql_dir_supports_installed_wheel_layout(tmp_path, monkeypatch):
    """Top-level wheel data installs next to the package, not two levels above it."""
    import scrapeyard.storage.database as mod

    package_dir = tmp_path / "site-packages" / "scrapeyard"
    sql_dir = tmp_path / "site-packages" / "sql"
    package_dir.mkdir(parents=True)
    sql_dir.mkdir()
    (sql_dir / "001_create_jobs.sql").write_text("-- migration", encoding="utf-8")

    monkeypatch.setattr(mod.importlib.resources, "files", lambda _package: package_dir)

    assert mod._resolve_sql_dir() == sql_dir


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
        assert "deletion_requested_at" in columns
        assert "delete_results_on_delete" in columns
        assert "schedule_timezone" in columns
        assert "config_hash" in columns
        assert "current_trigger" in columns
        cursor = await db.execute("PRAGMA table_info(job_runs)")
        run_columns = {column[1]: column for column in await cursor.fetchall()}
        assert "heartbeat_at" in run_columns
        assert run_columns["heartbeat_at"][3] == 1
        assert "webhook_reconciled_at" in run_columns
        assert "webhook_reconciliation_failed_at" in run_columns
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
        cursor = await db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name='idx_results_meta_project_run'"
        )
        assert await cursor.fetchone() is not None
        cursor = await db.execute(
            "SELECT metadata_cursor, filesystem_project "
            "FROM result_reconciliation_state WHERE singleton = 1"
        )
        assert tuple(await cursor.fetchone()) == (0, None)


async def test_init_db_idempotent(tmp_path):
    """Calling init_db twice should not raise."""
    db_dir = tmp_path / "db"
    await init_db(str(db_dir))
    await init_db(str(db_dir))


async def test_init_db_records_ordered_migration_history_once(tmp_path):
    db_dir = tmp_path / "db"
    await init_db(str(db_dir))

    histories: dict[str, list[tuple[str, str, str]]] = {}
    for db_name in ("jobs.db", "errors.db", "results_meta.db"):
        async with get_db(db_name) as db:
            cursor = await db.execute(
                "SELECT migration_id, filename, applied_at "
                "FROM schema_migrations ORDER BY migration_id"
            )
            histories[db_name] = [tuple(row) for row in await cursor.fetchall()]

    await init_db(str(db_dir))

    assert [row[0] for row in histories["jobs.db"]] == [
        "001",
        "004",
        "005",
        "009",
        "010",
        "012",
        "013",
        "014",
        "015",
        "016",
            "017",
            "018",
            "019",
        ]
    assert [row[0] for row in histories["errors.db"]] == ["002", "007"]
    assert [row[0] for row in histories["results_meta.db"]] == [
        "003",
        "006",
        "008",
        "011",
        "020",
    ]
    async with get_db("jobs.db") as db:
        cursor = await db.execute(
            "SELECT migration_id, filename, applied_at FROM schema_migrations ORDER BY migration_id"
        )
    assert [tuple(row) for row in await cursor.fetchall()] == histories["jobs.db"]


async def test_webhook_failure_reason_constraint_accepts_only_known_values(tmp_path):
    await init_db(str(tmp_path / "db"))
    async with get_db("jobs.db") as db:
        cursor = await db.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'webhook_deliveries'"
        )
        schema_sql = str((await cursor.fetchone())[0])
        insert_sql = """INSERT INTO webhook_deliveries
            (delivery_id, job_id, event, url, headers_json, payload_json,
             status, next_attempt_at, failure_reason, created_at, updated_at)
            VALUES (?, 'job', 'job.failed', '', '{}', '{}', 'failed',
                    '2026-01-01T00:00:00+00:00', ?,
                    '2026-01-01T00:00:00+00:00',
                    '2026-01-01T00:00:00+00:00')"""
        await db.execute(insert_sql, ("accepted", "decode_failure"))
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(insert_sql, ("rejected", "unknown_failure"))
        await db.rollback()

    assert "decode_failure" in schema_sql


async def test_webhook_decode_reason_migration_preserves_existing_rows(tmp_path):
    sql_dir = _resolve_sql_dir()
    migrations = _load_migrations(sql_dir)["jobs.db"]
    migration = next(item for item in migrations if item.migration_id == "016")
    async with aiosqlite.connect(tmp_path / "upgrade.db") as db:
        await db.executescript((sql_dir / "009_create_webhook_outbox.sql").read_text())
        await db.execute(
            """CREATE TABLE schema_migrations (
                   migration_id TEXT PRIMARY KEY,
                   filename TEXT NOT NULL UNIQUE,
                   checksum TEXT NOT NULL,
                   applied_at TEXT NOT NULL
               )"""
        )
        await db.execute(
            """INSERT INTO webhook_deliveries
                (delivery_id, job_id, event, url, headers_json, payload_json,
                 next_attempt_at, created_at, updated_at)
                VALUES ('existing', 'job', 'job.complete', 'url', '{}', '{}',
                        '2026-01-01T00:00:00+00:00',
                        '2026-01-01T00:00:00+00:00',
                        '2026-01-01T00:00:00+00:00')"""
        )
        await db.commit()

        await _apply_migration(db, migration)

        cursor = await db.execute(
            "SELECT job_id, status FROM webhook_deliveries WHERE delivery_id = 'existing'"
        )
        assert tuple(await cursor.fetchone()) == ("job", "pending")
        await db.execute(
            "UPDATE webhook_deliveries SET failure_reason = 'decode_failure' "
            "WHERE delivery_id = 'existing'"
        )
        await db.commit()


async def test_history_summary_migration_backfills_and_tracks_lifetime_runs(tmp_path):
    sql_dir = _resolve_sql_dir()
    migrations = _load_migrations(sql_dir)["jobs.db"]
    migration = next(item for item in migrations if item.migration_id == "017")
    async with aiosqlite.connect(tmp_path / "history-upgrade.db") as db:
        await db.executescript((sql_dir / "001_create_jobs.sql").read_text())
        await db.executescript((sql_dir / "004_create_job_runs.sql").read_text())
        await db.execute(
            """CREATE TABLE schema_migrations (
                   migration_id TEXT PRIMARY KEY,
                   filename TEXT NOT NULL UNIQUE,
                   checksum TEXT NOT NULL,
                   applied_at TEXT NOT NULL
               )"""
        )
        await db.execute(
            """INSERT INTO jobs
               (job_id, project, name, config_yaml, created_at)
               VALUES ('job', 'project', 'name', 'config', '2026-01-01')"""
        )
        await db.executemany(
            """INSERT INTO job_runs
               (run_id, job_id, trigger, config_hash, started_at, heartbeat_at)
               VALUES (?, 'job', 'scheduled', 'hash', ?, ?)""",
            [
                ("run-1", "2026-01-01", "2026-01-01"),
                ("run-2", "2026-01-03", "2026-01-03"),
            ],
        )
        await db.commit()

        await _apply_migration(db, migration)

        row = await (
            await db.execute(
                "SELECT lifetime_run_count, last_run_at FROM jobs WHERE job_id = 'job'"
            )
        ).fetchone()
        assert tuple(row) == (2, "2026-01-03")
        await db.execute(
            """INSERT INTO job_runs
               (run_id, job_id, trigger, config_hash, started_at, heartbeat_at)
               VALUES ('run-3', 'job', 'scheduled', 'hash',
                       '2026-01-02', '2026-01-02')"""
        )
        await db.execute("DELETE FROM job_runs WHERE run_id = 'run-1'")
        await db.commit()
        row = await (
            await db.execute(
                "SELECT lifetime_run_count, last_run_at FROM jobs WHERE job_id = 'job'"
            )
        ).fetchone()
        assert tuple(row) == (3, "2026-01-03")


async def test_init_db_rejects_checksum_drift(tmp_path):
    db_dir = tmp_path / "db"
    await init_db(str(db_dir))
    async with get_db("jobs.db") as db:
        await db.execute(
            "UPDATE schema_migrations SET checksum = 'changed' WHERE migration_id = '001'"
        )
        await db.commit()

    with pytest.raises(RuntimeError, match="checksum drift"):
        await init_db(str(db_dir))


async def test_init_db_rejects_wrong_database_assignment(tmp_path):
    db_dir = tmp_path / "db"
    await init_db(str(db_dir))
    async with get_db("jobs.db") as db:
        await db.execute(
            """INSERT INTO schema_migrations
               (migration_id, filename, checksum, applied_at)
               VALUES ('002', '002_create_errors.sql', 'checksum', 'now')"""
        )
        await db.commit()

    with pytest.raises(RuntimeError, match="wrong database"):
        await init_db(str(db_dir))


async def test_init_db_rejects_ledger_gap(tmp_path):
    db_dir = tmp_path / "db"
    await init_db(str(db_dir))
    async with get_db("jobs.db") as db:
        await db.execute("DELETE FROM schema_migrations WHERE migration_id = '001'")
        await db.commit()

    with pytest.raises(RuntimeError, match="ledger gap"):
        await init_db(str(db_dir))


def test_load_migrations_rejects_unassigned_file(tmp_path):
    sql_dir = tmp_path / "sql"
    shutil.copytree(_resolve_sql_dir(), sql_dir)
    (sql_dir / "999_unassigned.sql").write_text("SELECT 1;", encoding="utf-8")

    with pytest.raises(RuntimeError, match="assignment mismatch"):
        _load_migrations(sql_dir)


def test_load_migrations_rejects_numeric_gap(tmp_path, monkeypatch):
    import scrapeyard.storage.database as mod

    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    (sql_dir / "001_first.sql").write_text("SELECT 1;", encoding="utf-8")
    (sql_dir / "003_third.sql").write_text("SELECT 3;", encoding="utf-8")
    monkeypatch.setattr(
        mod,
        "_DB_MIGRATIONS",
        {"jobs.db": ("001_first.sql", "003_third.sql")},
    )

    with pytest.raises(RuntimeError, match="numeric gap"):
        mod._load_migrations(sql_dir)


async def test_failed_migration_rolls_back_schema_and_ledger(tmp_path):
    db_path = tmp_path / "failed.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """CREATE TABLE schema_migrations (
                   migration_id TEXT PRIMARY KEY,
                   filename TEXT NOT NULL UNIQUE,
                   checksum TEXT NOT NULL,
                   applied_at TEXT NOT NULL
               )"""
        )
        await db.commit()
        migration = Migration(
            migration_id="010",
            filename="010_broken.sql",
            sql="CREATE TABLE partial_change (id INTEGER); INVALID SQL;",
            checksum="checksum",
        )

        with pytest.raises(aiosqlite.OperationalError):
            await _apply_migration(db, migration)

        cursor = await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='partial_change'"
        )
        assert await cursor.fetchone() is None
        cursor = await db.execute("SELECT COUNT(*) FROM schema_migrations")
        assert (await cursor.fetchone())[0] == 0


async def test_db_transaction_commits_on_success(tmp_path):
    async with aiosqlite.connect(tmp_path / "transaction.db") as db:
        await db.execute("CREATE TABLE values_table (value TEXT)")
        async with db_transaction(db):
            await db.execute("INSERT INTO values_table VALUES ('committed')")

        cursor = await db.execute("SELECT value FROM values_table")
        assert (await cursor.fetchone())[0] == "committed"
        assert not db.in_transaction


async def test_db_transaction_rolls_back_on_exception(tmp_path):
    async with aiosqlite.connect(tmp_path / "transaction.db") as db:
        await db.execute("CREATE TABLE values_table (value TEXT)")
        await db.commit()
        with pytest.raises(RuntimeError, match="failure"):
            async with db_transaction(db):
                await db.execute("INSERT INTO values_table VALUES ('rolled-back')")
                raise RuntimeError("failure")

        cursor = await db.execute("SELECT COUNT(*) FROM values_table")
        assert (await cursor.fetchone())[0] == 0
        assert not db.in_transaction


async def test_db_transaction_rolls_back_when_commit_fails(tmp_path, monkeypatch):
    async with aiosqlite.connect(tmp_path / "transaction.db") as db:
        await db.execute("CREATE TABLE values_table (value TEXT)")
        await db.commit()

        async def fail_commit() -> None:
            raise OSError("commit failed")

        monkeypatch.setattr(db, "commit", fail_commit)
        with pytest.raises(OSError, match="commit failed"):
            async with db_transaction(db):
                await db.execute("INSERT INTO values_table VALUES ('rolled-back')")

        cursor = await db.execute("SELECT COUNT(*) FROM values_table")
        assert (await cursor.fetchone())[0] == 0
        assert not db.in_transaction


async def test_db_transaction_rolls_back_on_cancellation(tmp_path):
    async with aiosqlite.connect(tmp_path / "transaction.db") as db:
        await db.execute("CREATE TABLE values_table (value TEXT)")
        await db.commit()
        with pytest.raises(asyncio.CancelledError):
            async with db_transaction(db, immediate=True):
                await db.execute("INSERT INTO values_table VALUES ('cancelled')")
                raise asyncio.CancelledError

        cursor = await db.execute("SELECT COUNT(*) FROM values_table")
        assert (await cursor.fetchone())[0] == 0
        assert not db.in_transaction


@pytest.mark.parametrize("cancellation_count", [1, 3])
async def test_db_transaction_cancelled_entry_preserves_cached_connection(
    tmp_path, cancellation_count
):
    db_dir = tmp_path / "db"
    await init_db(str(db_dir))
    begin_started = asyncio.Event()
    connection_reused = asyncio.Event()
    loop = asyncio.get_running_loop()

    def trace(sql):
        if sql == "BEGIN IMMEDIATE":
            loop.call_soon_threadsafe(begin_started.set)

    async with get_db("jobs.db") as cached_db:
        await cached_db.execute("CREATE TABLE values_table (value TEXT)")
        await cached_db.commit()
        await cached_db.set_trace_callback(trace)

    async def cancelled_writer():
        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
            pytest.fail("Cancelled transaction body must not run")

    async def next_writer():
        async with get_db("jobs.db") as db:
            assert db is cached_db
            connection_reused.set()
            async with db_transaction(db, immediate=True):
                await db.execute("INSERT INTO values_table VALUES ('reused')")

    async with aiosqlite.connect(db_dir / "jobs.db") as blocker:
        await blocker.execute("BEGIN IMMEDIATE")
        writer = asyncio.create_task(cancelled_writer())
        next_write = None
        try:
            await asyncio.wait_for(begin_started.wait(), timeout=2)
            for _ in range(cancellation_count):
                writer.cancel()
                await asyncio.sleep(0)
            next_write = asyncio.create_task(next_writer())
            await asyncio.sleep(0)
            released_before_cleanup = connection_reused.is_set()
        finally:
            await blocker.rollback()
            with pytest.raises(asyncio.CancelledError):
                await writer
            if next_write is not None:
                await next_write

    assert not released_before_cleanup
    async with get_db("jobs.db") as db:
        assert not db.in_transaction
        cursor = await db.execute("SELECT value FROM values_table")
        assert [row[0] for row in await cursor.fetchall()] == ["reused"]


async def test_db_transaction_accepts_explicit_early_rollback(tmp_path):
    async with aiosqlite.connect(tmp_path / "transaction.db") as db:
        await db.execute("CREATE TABLE values_table (value TEXT)")
        await db.commit()
        async with db_transaction(db, immediate=True):
            await db.execute("INSERT INTO values_table VALUES ('discarded')")
            await db.rollback()

        cursor = await db.execute("SELECT COUNT(*) FROM values_table")
        assert (await cursor.fetchone())[0] == 0
        assert not db.in_transaction


async def test_init_db_upgrades_existing_job_runs_heartbeat_idempotently(tmp_path):
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    async with aiosqlite.connect(db_dir / "jobs.db") as db:
        await db.executescript(
            """CREATE TABLE jobs (
                   job_id TEXT PRIMARY KEY,
                   project TEXT NOT NULL,
                   name TEXT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'queued',
                   config_yaml TEXT NOT NULL,
                   created_at TEXT NOT NULL,
                   updated_at TEXT,
                   schedule_cron TEXT,
                   schedule_enabled INTEGER NOT NULL DEFAULT 1,
                   current_run_id TEXT,
                   UNIQUE (project, name)
               );
               CREATE TABLE job_runs (
                   run_id TEXT PRIMARY KEY,
                   job_id TEXT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'running',
                   trigger TEXT NOT NULL,
                   config_hash TEXT NOT NULL,
                   started_at TEXT NOT NULL,
                   completed_at TEXT,
                   record_count INTEGER,
                   error_count INTEGER NOT NULL DEFAULT 0
               );
               INSERT INTO job_runs
                   (run_id, job_id, status, trigger, config_hash, started_at)
               VALUES
                   ('old-run', 'old-job', 'running', 'adhoc', 'hash',
                    '2026-07-10T12:00:00+00:00');
               INSERT INTO jobs
                   (job_id, project, name, status, config_yaml, created_at,
                    updated_at, schedule_cron, schedule_enabled, current_run_id)
               VALUES
                   ('old-job', 'legacy', 'adhoc', 'running', '{}',
                    '2026-07-10T12:00:00+00:00',
                    '2026-07-10T12:00:00+00:00', NULL, 1, 'old-run'),
                   ('old-scheduled-job', 'legacy', 'scheduled', 'queued', '{}',
                    '2026-07-10T12:00:00+00:00',
                    '2026-07-10T12:00:00+00:00', '*/5 * * * *', 1,
                    'old-scheduled-run');"""
        )
        await db.commit()

    await init_db(str(db_dir))
    await init_db(str(db_dir))
    await migrate_persisted_secrets()

    async with get_db("jobs.db") as db:
        cursor = await db.execute("PRAGMA table_info(jobs)")
        job_columns = [row[1] for row in await cursor.fetchall()]
        cursor = await db.execute("PRAGMA table_info(job_runs)")
        columns = [row[1] for row in await cursor.fetchall()]
        cursor = await db.execute("SELECT heartbeat_at FROM job_runs WHERE run_id = 'old-run'")
        row = await cursor.fetchone()
        cursor = await db.execute(
            "SELECT job_id, current_trigger FROM jobs ORDER BY job_id"
        )
        trigger_rows = [tuple(item) for item in await cursor.fetchall()]

    assert columns.count("heartbeat_at") == 1
    assert job_columns.count("deletion_requested_at") == 1
    assert job_columns.count("delete_results_on_delete") == 1
    assert job_columns.count("current_trigger") == 1
    assert row is not None
    assert row["heartbeat_at"] == "2026-07-10T12:00:00+00:00"
    assert trigger_rows == [
        ("old-job", "adhoc"),
        ("old-scheduled-job", None),
    ]

    store = SQLiteJobStore()
    assert await store.claim_run(
        "old-scheduled-run",
        "old-scheduled-job",
        "manual",
        hashlib.sha256(b"{}").hexdigest(),
        datetime(2026, 7, 10, 12, 1, tzinfo=timezone.utc),
    )
    claimed = await store.get_job("old-scheduled-job")
    run = await store.get_job_run("old-scheduled-job", "old-scheduled-run")
    assert claimed.current_trigger == "manual"
    assert run is not None
    assert run.trigger == "manual"


async def test_init_db_upgrades_existing_webhook_outbox_item06_columns(tmp_path):
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    async with aiosqlite.connect(db_dir / "jobs.db") as db:
        await db.executescript(
            """CREATE TABLE webhook_deliveries (
                   delivery_id TEXT PRIMARY KEY,
                   job_id TEXT NOT NULL,
                   run_id TEXT,
                   event TEXT NOT NULL,
                   url TEXT NOT NULL,
                   headers_json TEXT NOT NULL DEFAULT '{}',
                   timeout_seconds REAL NOT NULL DEFAULT 10,
                   payload_json TEXT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'pending',
                   attempts INTEGER NOT NULL DEFAULT 0,
                   next_attempt_at TEXT NOT NULL,
                   last_attempt_at TEXT,
                   delivered_at TEXT,
                   last_error TEXT,
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL
               );
               INSERT INTO webhook_deliveries
                   (delivery_id, job_id, run_id, event, url, payload_json,
                    status, attempts, next_attempt_at, last_attempt_at,
                    last_error, created_at, updated_at)
               VALUES
                   ('legacy-failed', 'job-1', 'run-1', 'job.failed',
                    'https://hooks.example.com', '{}', 'failed', 1,
                    '2026-07-10T12:00:00+00:00',
                    '2026-07-10T12:00:01+00:00', 'HTTP 400',
                    '2026-07-10T12:00:00+00:00',
                    '2026-07-10T12:00:01+00:00');"""
        )
        await db.commit()

    await init_db(str(db_dir))
    await init_db(str(db_dir))

    async with get_db("jobs.db") as db:
        cursor = await db.execute("PRAGMA table_info(webhook_deliveries)")
        columns = [row[1] for row in await cursor.fetchall()]
        cursor = await db.execute(
            """SELECT failed_at, failure_reason, scrubbed_at
               FROM webhook_deliveries WHERE delivery_id = 'legacy-failed'"""
        )
        row = await cursor.fetchone()

    assert columns.count("failed_at") == 1
    assert columns.count("failure_reason") == 1
    assert columns.count("scrubbed_at") == 1
    assert row is not None
    assert row["failed_at"] == "2026-07-10T12:00:01+00:00"
    assert row["failure_reason"] == "non_retryable_failure"
    assert row["scrubbed_at"] is None


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
