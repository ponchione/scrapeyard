"""Query plans and results of the periodic SQLite queries tuned to use indexes.

Each rewritten query is checked two ways: its plan reaches rows through an
index instead of scanning history, and on seeded edge rows it returns exactly
what the previous form of the query returned.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import pytest

import scrapeyard.storage.error_store as error_store_module
import scrapeyard.storage.job_store as job_store_module
from scrapeyard.models.job import ErrorFilters
from scrapeyard.storage.database import get_db, init_db
from scrapeyard.storage.error_store import SQLiteErrorStore
from scrapeyard.storage.job_store import SQLiteJobStore

_NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)

# The queries as they were before that tuning, kept as the reference for results.
_OLD_TERMINAL_CANDIDATES = """SELECT job_runs.job_id,
       job_runs.run_id,
       job_runs.status,
       job_runs.config_hash,
       job_runs.config_yaml AS run_config_yaml,
       job_runs.started_at,
       job_runs.heartbeat_at,
       job_runs.completed_at,
       job_runs.record_count,
       job_runs.error_count,
       job_runs.webhook_reconciliation_failed_at,
       jobs.project,
       jobs.name,
       jobs.config_yaml AS parent_config_yaml,
       jobs.status AS parent_status,
       jobs.current_run_id,
       webhook_deliveries.delivery_id AS existing_delivery_id,
       webhook_deliveries.status AS existing_delivery_status,
       webhook_deliveries.scrubbed_at AS existing_delivery_scrubbed_at
FROM job_runs
JOIN jobs ON jobs.job_id = job_runs.job_id
LEFT JOIN webhook_deliveries
  ON webhook_deliveries.delivery_id = (
      SELECT candidate_delivery.delivery_id
      FROM webhook_deliveries AS candidate_delivery
      WHERE candidate_delivery.job_id = job_runs.job_id
        AND candidate_delivery.run_id = job_runs.run_id
        AND candidate_delivery.event = 'job.' || job_runs.status
      ORDER BY candidate_delivery.created_at ASC,
               candidate_delivery.delivery_id ASC
      LIMIT 1
  )
WHERE job_runs.status IN ('complete', 'partial', 'failed')
  AND jobs.status != 'deleting'
  AND (
      job_runs.webhook_reconciled_at IS NULL
      OR (
          jobs.status = 'running'
          AND jobs.current_run_id = job_runs.run_id
      )
  )
ORDER BY
         CASE
             WHEN job_runs.webhook_reconciliation_failed_at IS NULL
             THEN 0 ELSE 1
         END ASC,
         COALESCE(
             job_runs.webhook_reconciliation_failed_at,
             job_runs.completed_at,
             job_runs.started_at
         ) ASC,
         job_runs.job_id ASC,
         job_runs.run_id ASC"""

_OLD_STALE_QUEUED = """SELECT jobs.job_id, jobs.current_run_id,
       jobs.current_trigger,
       jobs.config_yaml AS parent_config_yaml,
       queued_run_snapshots.config_yaml AS queued_config_yaml,
       jobs.updated_at, jobs.schedule_cron,
       jobs.schedule_enabled
FROM jobs
LEFT JOIN queued_run_snapshots
  ON queued_run_snapshots.job_id = jobs.job_id
 AND queued_run_snapshots.run_id = jobs.current_run_id
WHERE jobs.status = 'queued'
  AND jobs.current_run_id IS NOT NULL
  AND jobs.updated_at IS NOT NULL
  AND jobs.updated_at <= ?
ORDER BY jobs.updated_at ASC, jobs.job_id ASC LIMIT ? OFFSET ?"""

_OLD_SCHEDULED_FROM = "FROM job_runs JOIN jobs ON jobs.job_id = job_runs.job_id WHERE "


class _RecordingConnection:
    """Forward to a connection, keeping each statement and its parameters."""

    def __init__(self, db: Any, statements: list[tuple[str, tuple[Any, ...]]]) -> None:
        self._db = db
        self._statements = statements

    def execute(self, sql: str, parameters: Any = ()) -> Any:
        self._statements.append((sql, tuple(parameters or ())))
        return self._db.execute(sql, parameters)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)


@pytest.fixture()
async def statements(tmp_path, monkeypatch) -> list[tuple[str, tuple[Any, ...]]]:
    await init_db(str(tmp_path / "db"))
    recorded: list[tuple[str, tuple[Any, ...]]] = []

    @asynccontextmanager
    async def recording_get_db(db_name: str) -> AsyncIterator[_RecordingConnection]:
        async with get_db(db_name) as db:
            yield _RecordingConnection(db, recorded)

    monkeypatch.setattr(job_store_module, "get_db", recording_get_db)
    monkeypatch.setattr(error_store_module, "get_db", recording_get_db)
    return recorded


def _statement(
    statements: list[tuple[str, tuple[Any, ...]]],
    marker: str,
) -> tuple[str, tuple[Any, ...]]:
    matches = [entry for entry in statements if marker in entry[0]]
    assert matches, f"no executed statement contains {marker!r}"
    return matches[-1]


async def _plan(db_name: str, sql: str, params: tuple[Any, ...]) -> list[str]:
    async with get_db(db_name) as db:
        cursor = await db.execute("EXPLAIN QUERY PLAN " + sql, params)
        return [str(row[3]) for row in await cursor.fetchall()]


async def _rows(db_name: str, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
    async with get_db(db_name) as db:
        cursor = await db.execute(sql, params)
        return [tuple(row) for row in await cursor.fetchall()]


def _ts(day: int, hour: int = 0) -> str:
    return datetime(2026, 9, day, hour, tzinfo=timezone.utc).isoformat()


async def _seed(sql: str, rows: list[tuple[Any, ...]]) -> None:
    async with get_db("jobs.db") as db:
        await db.executemany(sql, rows)
        await db.commit()


_INSERT_JOB = """INSERT INTO jobs
    (job_id, project, name, status, config_yaml, created_at, updated_at,
     schedule_cron, current_run_id, delete_results_on_delete)
    VALUES (?, 'acme', ?, ?, 'config', ?, ?, ?, ?, ?)"""
_INSERT_RUN = """INSERT INTO job_runs
    (run_id, job_id, status, trigger, config_hash, started_at, heartbeat_at,
     completed_at, webhook_reconciled_at, webhook_reconciliation_failed_at)
    VALUES (?, ?, ?, 'adhoc', 'hash', ?, ?, ?, ?, ?)"""
_INSERT_DELIVERY = """INSERT INTO webhook_deliveries
    (delivery_id, job_id, run_id, event, url, payload_json, status,
     next_attempt_at, scrubbed_at, created_at, updated_at)
    VALUES (?, ?, ?, ?, 'https://hooks.example/', '{}', ?, ?, ?, ?, ?)"""


async def test_terminal_candidates_use_partial_indexes_and_keep_results(statements):
    store = SQLiteJobStore()
    await _seed(_INSERT_JOB, [
        # job_id, name, status, created_at, updated_at, schedule_cron, current_run_id, delete
        ("unreconciled", "a", "complete", _ts(1), _ts(1), None, "r-unreconciled", None),
        ("reconciled", "b", "complete", _ts(1), _ts(1), None, "r-reconciled", None),
        ("converging", "c", "running", _ts(1), _ts(1), None, "r-current", None),
        ("deleting", "d", "deleting", _ts(1), _ts(1), None, "r-deleting", 1),
        ("live", "e", "running", _ts(1), _ts(1), None, "r-live", None),
        ("retrying", "f", "queued", _ts(1), _ts(1), None, "r-next", None),
        ("orphan-parent", "g", "running", _ts(1), _ts(1), None, None, None),
        ("borrowed", "h", "running", _ts(1), _ts(1), None, "r-unreconciled", None),
    ])
    await _seed(_INSERT_RUN, [
        # run_id, job_id, status, started, heartbeat, completed, reconciled, failed_at
        ("r-unreconciled", "unreconciled", "complete", _ts(2), _ts(2), _ts(3), None, None),
        ("r-reconciled", "reconciled", "complete", _ts(2), _ts(2), _ts(3), _ts(3), None),
        ("r-earlier", "converging", "failed", _ts(1), _ts(1), _ts(1), _ts(1), None),
        ("r-current", "converging", "partial", _ts(2), _ts(2), None, _ts(3), None),
        ("r-deleting", "deleting", "complete", _ts(2), _ts(2), _ts(2), None, None),
        ("r-live", "live", "running", _ts(2), _ts(2), None, None, None),
        ("r-retry", "retrying", "failed", _ts(2), _ts(2), _ts(4), None, _ts(5)),
        ("r-orphan", "orphan-parent", "complete", _ts(2), _ts(2), _ts(2), None, None),
    ])
    await _seed(_INSERT_DELIVERY, [
        ("d-late", "unreconciled", "r-unreconciled", "job.complete", "delivered",
         _ts(4), _ts(4), _ts(4, 2), _ts(4)),
        ("d-early", "unreconciled", "r-unreconciled", "job.complete", "pending",
         _ts(4), None, _ts(4, 1), _ts(4)),
        ("d-other-event", "unreconciled", "r-unreconciled", "job.failed", "pending",
         _ts(4), None, _ts(4, 0), _ts(4)),
    ])

    for limit in (None, 2):
        statements.clear()
        await store.list_terminal_webhook_candidates(limit=limit)
        sql, params = _statement(statements, "candidate_runs")

        new_rows = await _rows("jobs.db", sql, params)
        old_sql = _OLD_TERMINAL_CANDIDATES + ("" if limit is None else " LIMIT ?")
        assert new_rows == await _rows("jobs.db", old_sql, params)
        expected = [
            ("converging", "r-current"),
            ("orphan-parent", "r-orphan"),
            ("unreconciled", "r-unreconciled"),
            ("retrying", "r-retry"),
        ]
        assert [row[:2] for row in new_rows] == expected[:limit]
        if limit is None:
            assert new_rows[2][16] == "d-early"

        plan = await _plan("jobs.db", sql, params)
        assert any(
            "idx_job_runs_terminal_reconciliation (status=? AND webhook_reconciled_at=?)" in line
            for line in plan
        )
        assert any("idx_jobs_admission" in line for line in plan)
        assert not any(line.startswith(("SCAN job_runs", "SCAN jobs")) for line in plan)


async def test_stale_queued_jobs_use_admission_index_and_keep_results(statements):
    store = SQLiteJobStore()
    await _seed(_INSERT_JOB, [
        ("stale-b", "a", "queued", _ts(1), _ts(2), None, "r-b", None),
        ("stale-a", "b", "queued", _ts(1), _ts(2), None, "r-a", None),
        ("stale-scheduled", "c", "queued", _ts(1), _ts(1), "0 * * * *", "r-s", None),
        ("unowned", "d", "queued", _ts(1), _ts(1), "0 * * * *", None, None),
        ("no-timestamp", "e", "queued", _ts(1), None, None, "r-e", None),
        ("fresh", "f", "queued", _ts(1), _ts(20), None, "r-f", None),
        ("running", "g", "running", _ts(1), _ts(1), None, "r-g", None),
        ("done", "h", "complete", _ts(1), _ts(1), None, "r-h", None),
    ])
    async with get_db("jobs.db") as db:
        await db.execute(
            """INSERT INTO queued_run_snapshots
               (run_id, job_id, trigger, config_hash, config_yaml, queued_at)
               VALUES ('r-a', 'stale-a', 'adhoc', 'hash', 'snapshot', ?)""",
            (_ts(2),),
        )
        await db.commit()

    for limit, offset in ((100, 0), (2, 1)):
        statements.clear()
        stale = await store.list_stale_queued_jobs(
            datetime(2026, 9, 10, tzinfo=timezone.utc),
            limit=limit,
            offset=offset,
        )
        sql, params = _statement(statements, "AS queued_config_yaml")

        new_rows = await _rows("jobs.db", sql, params)
        assert new_rows == await _rows("jobs.db", _OLD_STALE_QUEUED, params)
        expected = ["stale-scheduled", "stale-a", "stale-b"][offset:]
        assert [row[0] for row in new_rows] == expected
        assert [job.job_id for job in stale] == expected

        plan = await _plan("jobs.db", sql, params)
        assert plan[0].startswith("SEARCH jobs USING INDEX idx_jobs_admission")


async def test_scheduled_history_scans_start_from_scheduled_jobs(statements):
    store = SQLiteJobStore()
    await _seed(_INSERT_JOB, [
        ("scheduled", "a", "complete", _ts(1), _ts(1), "0 * * * *", "s-current", None),
        ("other-scheduled", "b", "complete", _ts(1), _ts(1), "0 * * * *", None, None),
        ("adhoc", "c", "complete", _ts(1), _ts(1), None, "a-1", None),
    ])
    await _seed(_INSERT_RUN, [
        ("s-1", "scheduled", "complete", _ts(1), _ts(1), _ts(1), _ts(1), None),
        ("s-2", "scheduled", "failed", _ts(2), _ts(2), None, _ts(2), None),
        ("s-3", "scheduled", "partial", _ts(3), _ts(3), _ts(3), _ts(3), None),
        ("s-unreconciled", "scheduled", "complete", _ts(4), _ts(4), _ts(4), None, None),
        ("s-webhook", "scheduled", "complete", _ts(5), _ts(5), _ts(5), _ts(5), None),
        ("s-current", "scheduled", "complete", _ts(6), _ts(6), _ts(6), _ts(6), None),
        ("s-new", "scheduled", "complete", _ts(20), _ts(20), _ts(20), _ts(20), None),
        ("o-1", "other-scheduled", "complete", _ts(1), _ts(1), _ts(1), _ts(1), None),
        ("o-running", "other-scheduled", "running", _ts(2), _ts(2), None, None, None),
        ("a-1", "adhoc", "complete", _ts(1), _ts(1), _ts(1), _ts(1), None),
    ])
    await _seed(_INSERT_DELIVERY, [
        ("pending", "scheduled", "s-webhook", "job.complete", "pending",
         _ts(5), None, _ts(5), _ts(5)),
    ])
    expired_before = datetime(2026, 9, 10, tzinfo=timezone.utc)

    await store.summarize_cleanup_backlog(
        observed_at=_NOW,
        adhoc_expired_before=expired_before,
        scheduled_expired_before=expired_before,
        tombstone_expired_before=expired_before,
        max_scheduled_runs_per_job=2,
    )
    await store.list_scheduled_runs_for_retention(
        expired_before,
        tombstone_expired_before=expired_before,
        max_runs_per_job=2,
        limit=10,
    )
    count_sql, count_params = _statement(statements, "MIN(COALESCE(job_runs.completed_at")
    list_sql, list_params = _statement(statements, "SELECT job_runs.job_id, job_runs.run_id")

    for sql, params in ((count_sql, count_params), (list_sql, list_params)):
        old_sql = sql.replace(
            "FROM jobs CROSS JOIN job_runs ON job_runs.job_id = jobs.job_id WHERE ",
            _OLD_SCHEDULED_FROM,
        )
        assert await _rows("jobs.db", sql, params) == await _rows("jobs.db", old_sql, params)
        plan = await _plan("jobs.db", sql, params)
        assert plan[0] == "SCAN jobs USING INDEX idx_jobs_scheduled"
        assert not any("idx_job_runs_terminal_reconciliation" in line for line in plan)

    assert await _rows("jobs.db", list_sql, list_params) == [
        ("other-scheduled", "o-1"),
        ("scheduled", "s-1"),
        ("scheduled", "s-2"),
        ("scheduled", "s-3"),
    ]
    assert (await _rows("jobs.db", count_sql, count_params))[0][0] == 4


async def test_idempotency_lookups_by_job_use_job_index(statements):
    store = SQLiteJobStore()
    await _seed(_INSERT_JOB, [
        ("expired", "a", "complete", _ts(1), _ts(1), None, "r-1", None),
        ("deleting", "b", "deleting", _ts(1), _ts(1), None, "r-2", 0),
        ("queued", "c", "queued", _ts(1), _ts(1), None, "r-3", None),
    ])
    await _seed(
        """INSERT INTO scrape_idempotency
           (caller_scope, key_digest, request_hash, job_id, run_id,
            response_mode, created_at, expires_at)
           VALUES ('caller', ?, 'hash', ?, ?, 'async', ?, ?)""",
        [
            ("k-1", "expired", "r-1", _ts(1), _ts(2)),
            ("k-2", "deleting", "r-2", _ts(1), _ts(2)),
            ("k-3", "queued", "r-3", _ts(1), _ts(30)),
        ],
    )
    expired_before = datetime(2026, 9, 20, tzinfo=timezone.utc)

    assert await store.list_adhoc_jobs_for_retention(
        expired_before,
        idempotency_observed_at=_NOW,
        tombstone_expired_before=expired_before,
        limit=10,
    ) == ["deleting", "expired"]
    await store.finalize_job_deletion(
        "deleting",
        delete_results=False,
        preserve_idempotency_after=_NOW,
    )
    assert await store.rollback_queued_submission("queued", "r-3") is True

    for marker in (
        "AND scrape_idempotency.expires_at > ?",
        "WHERE job_id = ? AND expires_at > ?",
        "DELETE FROM scrape_idempotency WHERE job_id = ?",
        "DELETE FROM scrape_idempotency WHERE job_id = ? AND run_id = ?",
    ):
        sql, params = _statement(statements, marker)
        plan = await _plan("jobs.db", sql, params)
        assert any("idx_scrape_idempotency_job" in line for line in plan), (marker, plan)


async def test_project_and_job_error_query_uses_job_index(statements):
    store = SQLiteErrorStore()
    async with get_db("errors.db") as db:
        await db.executemany(
            """INSERT INTO errors
               (job_id, run_id, project, target_url, attempt, timestamp,
                error_type, http_status, fetcher_used, action_taken)
               VALUES (?, 'run', ?, 'https://example.com', 1, ?, 'http_error',
                       503, 'basic', 'retry')""",
            [
                ("job", "acme", _ts(1)),
                ("job", "acme", _ts(3)),
                ("job", "other", _ts(2)),
                ("other-job", "acme", _ts(4)),
            ],
        )
        await db.commit()

    records = await store.query_errors(ErrorFilters(project="acme", job_id="job"), limit=10)
    sql, params = _statement(statements, "FROM errors")

    assert [record.timestamp.day for record in records] == [3, 1]
    assert "+project = ?" in sql
    old_sql = sql.replace("+project = ?", "project = ?")
    assert await _rows("errors.db", sql, params) == await _rows("errors.db", old_sql, params)
    assert (await _plan("errors.db", sql, params))[0].startswith(
        "SEARCH errors USING INDEX idx_errors_job_timestamp (job_id=?)"
    )
