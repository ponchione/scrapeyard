"""Queued delivery and stale running-run recovery for the SQLite job store.

Each function runs on a jobs.db connection, and inside any transaction, that
:class:`~scrapeyard.storage.job_store.SQLiteJobStore` opens for it.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from datetime import datetime
from typing import cast

import aiosqlite

from scrapeyard.common.dt import fmt_dt, parse_dt
from scrapeyard.models.job import JobStatus
from scrapeyard.storage.job_terminal_reconciliation import TERMINAL_RUN_STATUSES
from scrapeyard.storage.secret_envelope import (
    SecretDecryptionError,
    protect_text,
    reveal_text,
)
from scrapeyard.storage.types import RunOwnershipError, RunRecovery, StaleQueuedJob

# This code moved out of job_store.py; its log lines keep that logger's name.
logger = logging.getLogger("scrapeyard.storage.job_store")


async def insert_failed_queued_run(
    db: aiosqlite.Connection,
    *,
    job_id: str,
    run_id: str,
    failed_at: datetime,
    error_count: int,
    failure_code: str,
) -> None:
    cursor = await db.execute(
        """SELECT queued_run_snapshots.trigger,
                      queued_run_snapshots.config_hash,
                      queued_run_snapshots.config_yaml AS queued_config_yaml,
                      queued_run_snapshots.queued_at,
                      jobs.config_hash AS parent_config_hash,
                      jobs.config_yaml AS parent_config_yaml,
                      jobs.current_trigger
               FROM jobs
               LEFT JOIN queued_run_snapshots
                 ON queued_run_snapshots.job_id = jobs.job_id
                AND queued_run_snapshots.run_id = jobs.current_run_id
               WHERE jobs.job_id = ? AND jobs.current_run_id = ?""",
        (job_id, run_id),
    )
    row = await cursor.fetchone()
    if row is None:
        raise RunOwnershipError("fail queued snapshot", job_id, run_id)

    config_hash = cast(str, row["config_hash"] or row["parent_config_hash"])
    config_yaml: str | None = None
    queued_config = cast(str | None, row["queued_config_yaml"])
    if queued_config is not None:
        try:
            config_yaml = reveal_text(
                queued_config,
                purpose=f"queued_run_snapshots.config_yaml:{run_id}",
            )
        except SecretDecryptionError:
            logger.exception(
                "Queued run snapshot could not be decrypted; falling back to "
                "the matching encrypted parent snapshot job_id=%s run_id=%s",
                job_id,
                run_id,
            )
    if config_yaml is None and row["parent_config_hash"] == config_hash:
        try:
            config_yaml = reveal_text(
                cast(str, row["parent_config_yaml"]),
                purpose=f"jobs.config_yaml:{job_id}",
            )
        except SecretDecryptionError:
            logger.exception(
                "Parent config fallback could not be decrypted for failed queued "
                "run job_id=%s run_id=%s",
                job_id,
                run_id,
            )
    if (
        config_yaml is not None
        and hashlib.sha256(config_yaml.encode("utf-8")).hexdigest() != config_hash
    ):
        config_yaml = None

    failed_text = fmt_dt(failed_at)
    started_text = cast(str | None, row["queued_at"]) or failed_text
    trigger = cast(str | None, row["trigger"] or row["current_trigger"]) or "adhoc"
    await db.execute(
        """INSERT INTO job_runs
                   (run_id, job_id, status, trigger, config_hash, config_yaml,
                    started_at, heartbeat_at, completed_at, record_count,
                    error_count, failure_code)
                   VALUES (?, ?, 'failed', ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
        (
            run_id,
            job_id,
            trigger,
            config_hash,
            (
                None
                if config_yaml is None
                else protect_text(
                    config_yaml,
                    purpose=f"job_runs.config_yaml:{run_id}",
                )
            ),
            started_text,
            started_text,
            failed_text,
            error_count,
            failure_code,
        ),
    )
    await db.execute(
        "DELETE FROM queued_run_snapshots WHERE job_id = ? AND run_id = ?",
        (job_id, run_id),
    )


async def stale_queued_rows(
    db: aiosqlite.Connection,
    stale_before: datetime,
    *,
    limit: int | None,
    offset: int,
) -> list[Mapping[str, object]]:
    # The redundant status IN (...) lets the partial idx_jobs_admission apply.
    query = """SELECT jobs.job_id, jobs.current_run_id,
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
                     AND jobs.status IN ('queued', 'running')
                     AND jobs.current_run_id IS NOT NULL
                     AND jobs.updated_at IS NOT NULL
                     AND jobs.updated_at <= ?
                   ORDER BY jobs.updated_at ASC, jobs.job_id ASC"""
    params: tuple[object, ...] = (fmt_dt(stale_before),)
    if limit is not None:
        query += " LIMIT ? OFFSET ?"
        params += (limit, offset)
    cursor = await db.execute(query, params)
    rows = cast(list[Mapping[str, object]], await cursor.fetchall())
    return rows


def stale_queued_jobs(rows: list[Mapping[str, object]]) -> list[StaleQueuedJob]:
    """Decrypt each stale row's config into a :class:`StaleQueuedJob`."""

    stale_jobs: list[StaleQueuedJob] = []
    for row in rows:
        queued_at = parse_dt(cast(str | None, row["updated_at"]))
        run_id = cast(str | None, row["current_run_id"])
        if queued_at is None or run_id is None:
            continue
        schedule_cron = cast(str | None, row["schedule_cron"])
        trigger = cast(str | None, row["current_trigger"])
        if trigger is None:
            # Pre-015 queued rows did not persist provenance. Preserve
            # the historical fallback only for this unresolvable upgrade
            # case; all newly accepted runs store an explicit trigger.
            trigger = "scheduled" if schedule_cron is not None else "adhoc"
        stale_jobs.append(
            StaleQueuedJob(
                job_id=cast(str, row["job_id"]),
                run_id=run_id,
                config_yaml=(
                    reveal_text(
                        cast(str, row["queued_config_yaml"]),
                        purpose=f"queued_run_snapshots.config_yaml:{run_id}",
                    )
                    if row["queued_config_yaml"] is not None
                    else reveal_text(
                        cast(str, row["parent_config_yaml"]),
                        purpose=f"jobs.config_yaml:{cast(str, row['job_id'])}",
                    )
                ),
                queued_at=queued_at,
                trigger=trigger,
                schedule_cron=schedule_cron,
                schedule_enabled=bool(row["schedule_enabled"]),
            )
        )
    return stale_jobs


async def reserve_queued_run_recovery(
    db: aiosqlite.Connection,
    job_id: str,
    run_id: str,
    *,
    expected_queued_at: datetime,
    stale_before: datetime,
    reserved_at: datetime,
) -> bool:
    cursor = await db.execute(
        """UPDATE jobs
                   SET updated_at = ?
                   WHERE job_id = ?
                     AND status = 'queued'
                     AND current_run_id = ?
                     AND updated_at = ?
                     AND updated_at <= ?""",
        (
            fmt_dt(reserved_at),
            job_id,
            run_id,
            fmt_dt(expected_queued_at),
            fmt_dt(stale_before),
        ),
    )
    if cursor.rowcount != 1:
        await db.rollback()
        return False
    return True


async def fail_queued_run_recovery(
    db: aiosqlite.Connection,
    job_id: str,
    run_id: str,
    *,
    expected_queued_at: datetime,
    failed_at: datetime,
) -> bool:
    cursor = await db.execute(
        """UPDATE jobs
                   SET status = 'failed', updated_at = ?
                   WHERE job_id = ?
                     AND status = 'queued'
                     AND current_run_id = ?
                     AND updated_at = ?""",
        (
            fmt_dt(failed_at),
            job_id,
            run_id,
            fmt_dt(expected_queued_at),
        ),
    )
    if cursor.rowcount != 1:
        await db.rollback()
        return False
    await insert_failed_queued_run(
        db,
        job_id=job_id,
        run_id=run_id,
        failed_at=failed_at,
        error_count=1,
        failure_code="queued_reconciliation_failure",
    )
    return True


def _heartbeat_is_stale(value: str | None, cutoff: datetime) -> bool:
    heartbeat = parse_dt(value)
    if heartbeat is None:
        return True
    comparable_cutoff = cutoff.replace(tzinfo=None) if heartbeat.tzinfo is None else cutoff
    return heartbeat <= comparable_cutoff


async def _recover_current_run(
    db: aiosqlite.Connection,
    *,
    job_id: str,
    expected_run_id: str | None,
    heartbeat_cutoff: datetime,
    recovered_at: datetime,
) -> RunRecovery | None:
    cursor = await db.execute(
        """SELECT jobs.status AS job_status,
                      jobs.current_run_id AS current_run_id,
                      job_runs.status AS run_status,
                      job_runs.heartbeat_at AS heartbeat_at
               FROM jobs
               LEFT JOIN job_runs
                 ON job_runs.job_id = jobs.job_id
                AND job_runs.run_id = jobs.current_run_id
               WHERE jobs.job_id = ?""",
        (job_id,),
    )
    row = await cursor.fetchone()
    if (
        row is None
        or row["job_status"] != JobStatus.running.value
        or row["current_run_id"] != expected_run_id
    ):
        return None

    run_id = cast(str | None, row["current_run_id"])
    run_status = cast(str | None, row["run_status"])
    heartbeat_text = cast(str | None, row["heartbeat_at"])
    last_heartbeat = parse_dt(heartbeat_text)
    recovered_text = fmt_dt(recovered_at)

    if run_id is None or run_status is None:
        cursor = await db.execute(
            """UPDATE jobs
                   SET status = 'failed', updated_at = ?
                   WHERE job_id = ?
                     AND status = 'running'
                     AND current_run_id IS ?""",
            (recovered_text, job_id, run_id),
        )
        if cursor.rowcount != 1:
            return None
        return RunRecovery(job_id, run_id, "failed_missing_run", last_heartbeat)

    if run_status in TERMINAL_RUN_STATUSES:
        cursor = await db.execute(
            """UPDATE jobs
                   SET status = ?, updated_at = ?
                   WHERE job_id = ?
                     AND status = 'running'
                     AND current_run_id = ?""",
            (run_status, recovered_text, job_id, run_id),
        )
        if cursor.rowcount != 1:
            return None
        return RunRecovery(job_id, run_id, "reconciled_terminal_run", last_heartbeat)

    if run_status != JobStatus.running.value or not _heartbeat_is_stale(
        heartbeat_text,
        heartbeat_cutoff,
    ):
        return None

    cursor = await db.execute(
        """UPDATE job_runs
               SET status = 'failed', completed_at = ?
               WHERE job_id = ?
                 AND run_id = ?
                 AND status = 'running'""",
        (recovered_text, job_id, run_id),
    )
    if cursor.rowcount != 1:
        return None
    cursor = await db.execute(
        """UPDATE jobs
               SET status = 'failed', updated_at = ?
               WHERE job_id = ?
                 AND status = 'running'
                 AND current_run_id = ?""",
        (recovered_text, job_id, run_id),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("Stale recovery lost job ownership inside transaction")
    return RunRecovery(job_id, run_id, "failed_stale_heartbeat", last_heartbeat)


async def recover_stale_run(
    db: aiosqlite.Connection,
    job_id: str,
    run_id: str | None,
    heartbeat_cutoff: datetime,
    recovered_at: datetime,
) -> bool:
    recovery = await _recover_current_run(
        db,
        job_id=job_id,
        expected_run_id=run_id,
        heartbeat_cutoff=heartbeat_cutoff,
        recovered_at=recovered_at,
    )
    return recovery is not None


async def recover_stale_running_jobs(
    db: aiosqlite.Connection,
    cutoff: datetime,
    recovered_at: datetime,
    limit: int | None,
) -> list[RunRecovery]:
    query = """SELECT jobs.job_id, jobs.current_run_id
                       FROM jobs
                       LEFT JOIN job_runs
                         ON job_runs.job_id = jobs.job_id
                        AND job_runs.run_id = jobs.current_run_id
                       WHERE jobs.status = 'running'
                         AND (
                             job_runs.run_id IS NULL
                             OR job_runs.status != 'running'
                             OR job_runs.heartbeat_at IS NULL
                             OR job_runs.heartbeat_at <= ?
                         )
                       ORDER BY COALESCE(job_runs.heartbeat_at, jobs.updated_at) ASC,
                                jobs.job_id ASC"""
    parameters: tuple[object, ...] = (fmt_dt(cutoff),)
    if limit is not None:
        if limit < 1:
            raise ValueError("limit must be positive")
        query += " LIMIT ?"
        parameters = (*parameters, limit)
    cursor = await db.execute(query, parameters)
    rows = await cursor.fetchall()
    recoveries: list[RunRecovery] = []
    for row in rows:
        recovery = await _recover_current_run(
            db,
            job_id=cast(str, row["job_id"]),
            expected_run_id=cast(str | None, row["current_run_id"]),
            heartbeat_cutoff=cutoff,
            recovered_at=recovered_at,
        )
        if recovery is not None:
            recoveries.append(recovery)
    return recoveries
