"""Job and scheduled-run history retention for the SQLite job store.

Each function runs on a jobs.db connection, and inside any transaction, that
:class:`~scrapeyard.storage.job_store.SQLiteJobStore` opens for it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import aiosqlite

from scrapeyard.common.dt import fmt_dt, parse_dt
from scrapeyard.storage.types import CleanupBacklogSnapshot, HistoryPruneResult


async def cleanup_backlog_rows(
    db: aiosqlite.Connection,
    *,
    observed_at: datetime,
    adhoc_expired_before: datetime,
    scheduled_expired_before: datetime,
    tombstone_expired_before: datetime,
    max_scheduled_runs_per_job: int,
) -> tuple[Any, Any, Any]:
    """Count the idempotency records, ad-hoc jobs and scheduled runs due for cleanup."""

    idempotency_cursor = await db.execute(
        """SELECT COUNT(*) AS eligible_count,
                          MIN(expires_at) AS oldest_eligible_at
                   FROM scrape_idempotency
                   WHERE expires_at <= ?""",
        (fmt_dt(observed_at),),
    )
    idempotency_row = await idempotency_cursor.fetchone()
    adhoc_cursor = await db.execute(
        "SELECT COUNT(*) AS eligible_count, "
        "MIN(COALESCE(jobs.deletion_requested_at, jobs.updated_at, jobs.created_at)) "
        "AS oldest_eligible_at FROM jobs WHERE "
        + adhoc_retention_eligibility_sql(),
        (
            fmt_dt(adhoc_expired_before),
            fmt_dt(tombstone_expired_before),
            fmt_dt(observed_at),
        ),
    )
    adhoc_row = await adhoc_cursor.fetchone()
    scheduled_cursor = await db.execute(
        "SELECT COUNT(*) AS eligible_count, "
        "MIN(COALESCE(job_runs.completed_at, job_runs.started_at)) "
        "AS oldest_eligible_at "
        "FROM jobs CROSS JOIN job_runs ON job_runs.job_id = jobs.job_id WHERE "
        + scheduled_history_eligibility_sql(),
        (
            fmt_dt(tombstone_expired_before),
            fmt_dt(scheduled_expired_before),
            max_scheduled_runs_per_job,
        ),
    )
    scheduled_row = await scheduled_cursor.fetchone()
    return idempotency_row, adhoc_row, scheduled_row


def cleanup_backlog_snapshots(
    idempotency_row: Any,
    adhoc_row: Any,
    scheduled_row: Any,
) -> dict[str, CleanupBacklogSnapshot]:
    """Turn the :func:`cleanup_backlog_rows` counts into backlog snapshots."""

    assert (
        idempotency_row is not None
        and adhoc_row is not None
        and scheduled_row is not None
    )
    return {
        "idempotency_records": CleanupBacklogSnapshot(
            int(idempotency_row["eligible_count"]),
            parse_dt(idempotency_row["oldest_eligible_at"]),
        ),
        "adhoc_jobs": CleanupBacklogSnapshot(
            int(adhoc_row["eligible_count"]),
            parse_dt(adhoc_row["oldest_eligible_at"]),
        ),
        "scheduled_runs": CleanupBacklogSnapshot(
            int(scheduled_row["eligible_count"]),
            parse_dt(scheduled_row["oldest_eligible_at"]),
        ),
    }


async def list_adhoc_jobs_for_retention(
    db: aiosqlite.Connection,
    expired_before: datetime,
    *,
    idempotency_observed_at: datetime,
    tombstone_expired_before: datetime,
    limit: int,
) -> list[str]:
    cursor = await db.execute(
        "SELECT jobs.job_id FROM jobs WHERE "
        + adhoc_retention_eligibility_sql()
        + """
                   ORDER BY COALESCE(
                                jobs.deletion_requested_at,
                                jobs.updated_at,
                                jobs.created_at
                            ),
                            jobs.job_id
                   LIMIT ?""",
        (
            fmt_dt(expired_before),
            fmt_dt(tombstone_expired_before),
            fmt_dt(idempotency_observed_at),
            limit,
        ),
    )
    return [str(row["job_id"]) for row in await cursor.fetchall()]


def adhoc_retention_eligibility_sql() -> str:
    """Ad-hoc jobs safe to compact; parameters: expired, tombstone, observed."""
    return """
            jobs.schedule_cron IS NULL
            AND (
                (
                    jobs.status IN ('complete', 'partial', 'failed', 'cancelled')
                    AND COALESCE(jobs.updated_at, jobs.created_at) <= ?
                )
                OR (
                    jobs.status = 'deleting'
                    AND jobs.delete_results_on_delete = 0
                )
            )
            AND NOT EXISTS (
                SELECT 1 FROM webhook_deliveries
                WHERE webhook_deliveries.job_id = jobs.job_id
                  AND (
                      webhook_deliveries.status NOT IN ('delivered', 'failed')
                      OR webhook_deliveries.scrubbed_at IS NULL
                      OR webhook_deliveries.scrubbed_at > ?
                  )
            )
            AND NOT EXISTS (
                SELECT 1 FROM scrape_idempotency
                WHERE scrape_idempotency.job_id = jobs.job_id
                  AND scrape_idempotency.expires_at > ?
            )
        """


def scheduled_history_eligibility_sql() -> str:
    """Scheduled runs safe to prune; parameters: tombstone, expired, count.

        Callers join ``FROM jobs CROSS JOIN job_runs`` so the scan starts from
        the few scheduled jobs instead of every terminal run.
        """
    return """
            jobs.schedule_cron IS NOT NULL
            AND job_runs.status IN ('complete', 'partial', 'failed')
            AND job_runs.run_id IS NOT jobs.current_run_id
            AND job_runs.webhook_reconciled_at IS NOT NULL
            AND NOT EXISTS (
                SELECT 1 FROM webhook_deliveries
                WHERE webhook_deliveries.job_id = job_runs.job_id
                  AND webhook_deliveries.run_id = job_runs.run_id
                  AND (
                      webhook_deliveries.status NOT IN ('delivered', 'failed')
                      OR webhook_deliveries.scrubbed_at IS NULL
                      OR webhook_deliveries.scrubbed_at > ?
                  )
            )
            AND (
                COALESCE(job_runs.completed_at, job_runs.started_at) <= ?
                OR (
                    SELECT COUNT(*)
                    FROM job_runs AS newer_runs
                    WHERE newer_runs.job_id = job_runs.job_id
                      AND newer_runs.status IN ('complete', 'partial', 'failed')
                      AND (
                          newer_runs.started_at > job_runs.started_at
                          OR (
                              newer_runs.started_at = job_runs.started_at
                              AND newer_runs.run_id > job_runs.run_id
                          )
                      )
                ) >= ?
            )
        """


async def list_scheduled_runs_for_retention(
    db: aiosqlite.Connection,
    expired_before: datetime,
    *,
    tombstone_expired_before: datetime,
    max_runs_per_job: int,
    limit: int,
) -> list[tuple[str, str]]:
    query = (
        "SELECT job_runs.job_id, job_runs.run_id "
        "FROM jobs CROSS JOIN job_runs ON job_runs.job_id = jobs.job_id WHERE "
        + scheduled_history_eligibility_sql()
        + " ORDER BY COALESCE(job_runs.completed_at, job_runs.started_at), "
        "job_runs.job_id, job_runs.run_id LIMIT ?"
    )
    cursor = await db.execute(
        query,
        (
            fmt_dt(tombstone_expired_before),
            fmt_dt(expired_before),
            max_runs_per_job,
            limit,
        ),
    )
    return [
        (str(row["job_id"]), str(row["run_id"]))
        for row in await cursor.fetchall()
    ]


async def prune_scheduled_run_for_retention(
    db: aiosqlite.Connection,
    job_id: str,
    run_id: str,
    *,
    expired_before: datetime,
    tombstone_expired_before: datetime,
    max_runs_per_job: int,
) -> HistoryPruneResult:
    query = (
        "SELECT 1 FROM job_runs "
        "JOIN jobs ON jobs.job_id = job_runs.job_id "
        "WHERE job_runs.job_id = ? AND job_runs.run_id = ? AND "
        + scheduled_history_eligibility_sql()
    )
    cursor = await db.execute(
        query,
        (
            job_id,
            run_id,
            fmt_dt(tombstone_expired_before),
            fmt_dt(expired_before),
            max_runs_per_job,
        ),
    )
    if await cursor.fetchone() is None:
        await db.rollback()
        return HistoryPruneResult(False)
    tombstones = await db.execute(
        """DELETE FROM webhook_deliveries
                   WHERE job_id = ? AND run_id = ?
                     AND status IN ('delivered', 'failed')
                     AND scrubbed_at <= ?""",
        (job_id, run_id, fmt_dt(tombstone_expired_before)),
    )
    pruned = await db.execute(
        """DELETE FROM job_runs
                   WHERE job_id = ? AND run_id = ?
                     AND status IN ('complete', 'partial', 'failed')""",
        (job_id, run_id),
    )
    if pruned.rowcount != 1:
        raise RuntimeError("Scheduled history pruning lost its selected run")
    return HistoryPruneResult(True, tombstones.rowcount)
