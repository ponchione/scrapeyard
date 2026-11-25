"""Run lifecycle and ownership queries for jobs.db."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Protocol, cast

import aiosqlite

from scrapeyard.common.dt import fmt_dt
from scrapeyard.models.job import JobRun, JobStatus
from scrapeyard.storage import job_crud, job_recovery, job_terminal_reconciliation
from scrapeyard.storage.job_rows import row_to_job_run
from scrapeyard.storage.job_sql import JOB_RUN_COLUMNS, select_columns
from scrapeyard.storage.secret_envelope import protect_text
from scrapeyard.storage.types import RunOwnershipError
from scrapeyard.storage.webhook_outbox import WebhookDeliveryCreate

_RUN_IDENTITY_CACHE_ENTRIES = 256


class RevealVerifiedFn(Protocol):
    def __call__(self, value: str, *, purpose: str, expected_hash: str) -> str | None: ...


class RevealIdentityFn(Protocol):
    def __call__(
        self,
        value: str,
        *,
        purpose: str,
        expected_hash: str,
        cached_identity: tuple[object, object] | None,
    ) -> tuple[object, object] | None: ...


class RunStateCache:
    """Bounded LRU verification cache for exact run identity reads."""

    def __init__(self, *, max_entries: int = _RUN_IDENTITY_CACHE_ENTRIES) -> None:
        self._max_entries = max_entries
        self._run_identities: OrderedDict[str, tuple[object, object]] = OrderedDict()
        self._verified_runs: OrderedDict[
            tuple[str, str], tuple[str, str, str, object, object]
        ] = OrderedDict()

    async def resolve_state(
        self,
        row: Mapping[str, object] | None,
        *,
        job_id: str,
        run_id: str,
        reveal_identity: RevealIdentityFn,
    ) -> dict[str, object] | None:
        if row is None or row["config_yaml"] is None:
            return None
        state = dict(row)
        config_hash = cast(str, state["config_hash"])
        origin = str(state.pop("origin"))
        encrypted_config = str(state.pop("config_yaml"))
        ciphertext_digest = await job_crud.sha256_text_async(encrypted_config)
        run_key = (job_id, run_id)
        cached_run = self._verified_runs.get(run_key)
        if cached_run is not None and cached_run[:3] == (origin, config_hash, ciphertext_digest):
            project, name = cached_run[3], cached_run[4]
            if project != state["project"] or not isinstance(name, str):
                raise ValueError("Exact run configuration identity mismatch")
            self._verified_runs.move_to_end(run_key)
            state["name"] = name
            return state
        cached_identity = self._run_identities.get(config_hash)
        identity = await asyncio.to_thread(
            reveal_identity,
            encrypted_config,
            purpose=f"{origin}.config_yaml:{run_id}",
            expected_hash=config_hash,
            cached_identity=cached_identity,
        )
        if identity is None:
            raise ValueError("Exact run configuration hash mismatch")
        if cached_identity is None:
            self._run_identities[config_hash] = identity
            if len(self._run_identities) > self._max_entries:
                self._run_identities.popitem(last=False)
        else:
            self._run_identities.move_to_end(config_hash)
        project, name = identity
        if project != state["project"] or not isinstance(name, str):
            raise ValueError("Exact run configuration identity mismatch")
        self._verified_runs[run_key] = (origin, config_hash, ciphertext_digest, project, name)
        if len(self._verified_runs) > self._max_entries:
            self._verified_runs.popitem(last=False)
        state["name"] = name
        return state


async def run_is_active(db: aiosqlite.Connection, job_id: str, run_id: str) -> bool:
    """Check exact parent/run ownership at a cooperative lifecycle boundary."""

    cursor = await db.execute(
        """SELECT 1
           FROM jobs
           JOIN job_runs
             ON job_runs.job_id = jobs.job_id
            AND job_runs.run_id = jobs.current_run_id
           WHERE jobs.job_id = ?
             AND jobs.current_run_id = ?
             AND jobs.status = 'running'
             AND job_runs.status = 'running'""",
        (job_id, run_id),
    )
    return await cursor.fetchone() is not None


async def result_run_is_active(
    db: aiosqlite.Connection,
    project: str,
    job_name: str,
    run_id: str,
) -> bool:
    """Protect exact queued/running result ownership during reconciliation."""

    cursor = await db.execute(
        """SELECT 1
           FROM jobs
           WHERE project = ?
             AND name = ?
             AND current_run_id = ?
             AND status IN ('queued', 'running')
           LIMIT 1""",
        (project, job_name, run_id),
    )
    return await cursor.fetchone() is not None


async def get_job_runs(
    db: aiosqlite.Connection,
    job_id: str,
    limit: int = 10,
) -> list[JobRun]:
    """Return the last N runs for a job, newest first."""

    cursor = await db.execute(
        f"SELECT {select_columns(JOB_RUN_COLUMNS)} FROM job_runs WHERE job_id = ? "
        "ORDER BY started_at DESC LIMIT ?",
        (job_id, limit),
    )
    rows = cast(list[Mapping[str, object]], await cursor.fetchall())
    return [row_to_job_run(row) for row in rows]


async def get_job_run(
    db: aiosqlite.Connection,
    job_id: str,
    run_id: str,
) -> JobRun | None:
    """Return one run only when it belongs to the expected parent job."""

    cursor = await db.execute(
        f"SELECT {select_columns(JOB_RUN_COLUMNS)} FROM job_runs "
        "WHERE job_id = ? AND run_id = ?",
        (job_id, run_id),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return row_to_job_run(cast(Mapping[str, object], row))


async def fetch_job_run_state_row(
    db: aiosqlite.Connection,
    job_id: str,
    run_id: str,
) -> Mapping[str, object] | None:
    cursor = await db.execute(
        """SELECT j.job_id, j.project, j.name,
                  COALESCE(r.run_id, q.run_id) AS run_id,
                  CASE WHEN r.config_yaml IS NOT NULL THEN r.config_hash
                       ELSE COALESCE(q.config_hash, r.config_hash) END AS config_hash,
                  COALESCE(r.status, j.status) AS status,
                  r.started_at, r.completed_at, r.failure_code,
                  COALESCE(r.config_yaml, q.config_yaml) AS config_yaml,
                  CASE WHEN r.config_yaml IS NOT NULL THEN 'job_runs' ELSE 'queued_run_snapshots' END AS origin
           FROM jobs j
           LEFT JOIN job_runs r ON r.job_id = j.job_id AND r.run_id = ?
           LEFT JOIN queued_run_snapshots q
             ON q.job_id = j.job_id AND q.run_id = ? AND q.run_id = j.current_run_id
           WHERE j.job_id = ? AND (r.run_id IS NOT NULL OR q.run_id IS NOT NULL)""",
        (run_id, run_id, job_id),
    )
    row = await cursor.fetchone()
    return None if row is None else cast(Mapping[str, object], row)


async def get_queued_run_config(
    db: aiosqlite.Connection,
    job_id: str,
    run_id: str,
    *,
    reveal_verified: RevealVerifiedFn,
) -> str:
    """Load the encrypted immutable config for one accepted delivery."""

    cursor = await db.execute(
        """SELECT queued_run_snapshots.config_yaml,
                  queued_run_snapshots.config_hash
           FROM queued_run_snapshots
           JOIN jobs ON jobs.job_id = queued_run_snapshots.job_id
           WHERE queued_run_snapshots.job_id = ?
             AND queued_run_snapshots.run_id = ?
             AND jobs.status = 'queued'
             AND jobs.current_run_id = queued_run_snapshots.run_id""",
        (job_id, run_id),
    )
    row = await cursor.fetchone()
    if row is not None:
        config_yaml = reveal_verified(
            cast(str, row["config_yaml"]),
            purpose=f"queued_run_snapshots.config_yaml:{run_id}",
            expected_hash=cast(str, row["config_hash"]),
        )
        if config_yaml is None:
            raise ValueError("Queued run configuration hash mismatch")
        return config_yaml

    # Upgrade compatibility for deliveries accepted before migration 019.
    cursor = await db.execute(
        """SELECT config_yaml, config_hash FROM jobs
           WHERE job_id = ? AND status = 'queued' AND current_run_id = ?""",
        (job_id, run_id),
    )
    legacy = await cursor.fetchone()
    if legacy is None:
        raise RunOwnershipError("load queued config", job_id, run_id)
    config_yaml = reveal_verified(
        cast(str, legacy["config_yaml"]),
        purpose=f"jobs.config_yaml:{job_id}",
        expected_hash=cast(str, legacy["config_hash"]),
    )
    if config_yaml is None:
        raise ValueError("Legacy queued configuration hash mismatch")
    return config_yaml


async def claim_run(
    db: aiosqlite.Connection,
    run_id: str,
    job_id: str,
    trigger: str,
    config_hash: str,
    started_at: datetime,
    *,
    reveal_verified: RevealVerifiedFn,
) -> bool:
    """Atomically transition one queued delivery to an owned running run."""

    timestamp = fmt_dt(started_at)
    cursor = await db.execute(
        """UPDATE jobs
               SET status = 'running', updated_at = ?, current_trigger = ?
               WHERE job_id = ?
                 AND status = 'queued'
                 AND current_run_id IS ?
                 AND (current_trigger = ? OR current_trigger IS NULL)""",
        (timestamp, trigger, job_id, run_id, trigger),
    )
    if cursor.rowcount != 1:
        await db.rollback()
        return False
    cursor = await db.execute(
        """SELECT queued_run_snapshots.config_yaml,
                  queued_run_snapshots.config_hash,
                  queued_run_snapshots.trigger
           FROM queued_run_snapshots
           WHERE job_id = ? AND run_id = ?""",
        (job_id, run_id),
    )
    config_row = await cursor.fetchone()
    if config_row is None:
        # Upgrade compatibility for a delivery accepted before 019.
        cursor = await db.execute(
            "SELECT config_yaml, config_hash FROM jobs WHERE job_id = ?",
            (job_id,),
        )
        config_row = await cursor.fetchone()
        config_purpose = f"jobs.config_yaml:{job_id}"
    else:
        if config_row["trigger"] != trigger:
            await db.rollback()
            return False
        config_purpose = f"queued_run_snapshots.config_yaml:{run_id}"
    if config_row is None or config_row["config_hash"] != config_hash:
        await db.rollback()
        return False
    config_yaml = reveal_verified(
        cast(str, config_row["config_yaml"]),
        purpose=config_purpose,
        expected_hash=config_hash,
    )
    if config_yaml is None:
        await db.rollback()
        return False
    await db.execute(
        """INSERT INTO job_runs
               (run_id, job_id, status, trigger, config_hash, config_yaml,
                started_at, heartbeat_at)
               VALUES (?, ?, 'running', ?, ?, ?, ?, ?)""",
        (
            run_id,
            job_id,
            trigger,
            config_hash,
            protect_text(
                config_yaml,
                purpose=f"job_runs.config_yaml:{run_id}",
            ),
            timestamp,
            timestamp,
        ),
    )
    await db.execute(
        "DELETE FROM queued_run_snapshots WHERE job_id = ? AND run_id = ?",
        (job_id, run_id),
    )
    return True


async def heartbeat_run(
    db: aiosqlite.Connection,
    job_id: str,
    run_id: str,
    heartbeat_at: datetime,
) -> None:
    """Refresh the expected active run without touching the parent timestamp."""

    cursor = await db.execute(
        """UPDATE job_runs
           SET heartbeat_at = ?
           WHERE job_id = ?
             AND run_id = ?
             AND status = 'running'
             AND EXISTS (
                 SELECT 1 FROM jobs
                 WHERE jobs.job_id = job_runs.job_id
                   AND jobs.status = 'running'
                   AND jobs.current_run_id = job_runs.run_id
             )""",
        (fmt_dt(heartbeat_at), job_id, run_id),
    )
    if cursor.rowcount != 1:
        raise RunOwnershipError("heartbeat", job_id, run_id)


async def finalize_owned_run(
    db: aiosqlite.Connection,
    job_id: str,
    run_id: str,
    status: str,
    record_count: int,
    error_count: int,
    completed_at: datetime,
    heartbeat_cutoff: datetime,
    *,
    webhook_delivery: WebhookDeliveryCreate | None = None,
) -> None:
    """Finalize the run, parent, and required intent in one CAS transaction."""

    completed_text = fmt_dt(completed_at)
    cutoff_text = fmt_dt(heartbeat_cutoff)
    cursor = await db.execute(
        """UPDATE job_runs
               SET status = ?, completed_at = ?,
                   record_count = ?, error_count = ?,
                   webhook_reconciled_at = ?
               WHERE job_id = ?
                 AND run_id = ?
                 AND status = 'running'
                 AND heartbeat_at > ?""",
        (
            status,
            completed_text,
            record_count,
            error_count,
            completed_text,
            job_id,
            run_id,
            cutoff_text,
        ),
    )
    if cursor.rowcount != 1:
        await db.rollback()
        raise RunOwnershipError("finalize", job_id, run_id)
    cursor = await db.execute(
        """UPDATE jobs
               SET status = ?, updated_at = ?
               WHERE job_id = ?
                 AND status = 'running'
                 AND current_run_id = ?""",
        (status, completed_text, job_id, run_id),
    )
    if cursor.rowcount != 1:
        await db.rollback()
        raise RunOwnershipError("finalize", job_id, run_id)
    await job_terminal_reconciliation.commit_terminal_intent(
        db,
        webhook_delivery,
        job_id=job_id,
        run_id=run_id,
        status=status,
        created_at=completed_at,
        checkpoint="during_webhook_intent_transaction",
    )


async def fail_owned_run(
    db: aiosqlite.Connection,
    job_id: str,
    run_id: str,
    failed_at: datetime,
    heartbeat_cutoff: datetime | None = None,
    *,
    error_count: int = 0,
    webhook_delivery: WebhookDeliveryCreate | None = None,
) -> None:
    """Fail matching ownership with optional atomic terminal intent."""

    failed_text = fmt_dt(failed_at)
    cursor = await db.execute(
        "SELECT status FROM jobs WHERE job_id = ? AND current_run_id = ?",
        (job_id, run_id),
    )
    row = await cursor.fetchone()
    if row is None:
        await db.rollback()
        raise RunOwnershipError("fail", job_id, run_id)
    job_status = str(row["status"])
    if job_status == JobStatus.queued.value:
        await job_recovery.insert_failed_queued_run(
            db,
            job_id=job_id,
            run_id=run_id,
            failed_at=failed_at,
            error_count=error_count,
            failure_code="preclaim_execution_failure",
        )
        cursor = await db.execute(
            """UPDATE jobs
                   SET status = 'failed', updated_at = ?
                   WHERE job_id = ?
                     AND status = 'queued'
                     AND current_run_id = ?""",
            (failed_text, job_id, run_id),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            raise RunOwnershipError("fail", job_id, run_id)
        await job_terminal_reconciliation.commit_terminal_intent(
            db,
            webhook_delivery,
            job_id=job_id,
            run_id=run_id,
            status=JobStatus.failed.value,
            created_at=failed_at,
        )
        return
    if job_status != JobStatus.running.value:
        await db.rollback()
        raise RunOwnershipError("fail", job_id, run_id)

    heartbeat_clause = ""
    params: list[object] = [
        failed_text,
        error_count,
        failed_text,
        job_id,
        run_id,
    ]
    if heartbeat_cutoff is not None:
        heartbeat_clause = " AND heartbeat_at > ?"
        params.append(fmt_dt(heartbeat_cutoff))
    cursor = await db.execute(
        """UPDATE job_runs
               SET status = 'failed', completed_at = ?,
                   record_count = COALESCE(record_count, 0),
                   error_count = ?, webhook_reconciled_at = ?
               WHERE job_id = ?
                 AND run_id = ?
                 AND status = 'running'"""
        + heartbeat_clause,
        params,
    )
    if cursor.rowcount != 1:
        await db.rollback()
        raise RunOwnershipError("fail", job_id, run_id)
    cursor = await db.execute(
        """UPDATE jobs
               SET status = 'failed', updated_at = ?
               WHERE job_id = ?
                 AND status = 'running'
                 AND current_run_id = ?""",
        (failed_text, job_id, run_id),
    )
    if cursor.rowcount != 1:
        await db.rollback()
        raise RunOwnershipError("fail", job_id, run_id)
    await job_terminal_reconciliation.commit_terminal_intent(
        db,
        webhook_delivery,
        job_id=job_id,
        run_id=run_id,
        status=JobStatus.failed.value,
        created_at=failed_at,
    )


async def queue_run(
    db: aiosqlite.Connection,
    job_id: str,
    *,
    expected_status: str,
    expected_run_id: str | None,
    new_run_id: str,
    new_trigger: str,
    queued_at: datetime,
    stale_before: datetime | None = None,
    expected_hash: str | None = None,
    prepared_snapshot: str | None = None,
    require_schedule_enabled: bool = False,
    check_admission: Callable[[aiosqlite.Connection], Awaitable[None]],
    reveal_verified: RevealVerifiedFn,
) -> bool:
    """Queue a replacement only if the scheduler's observed state still holds."""

    params: list[object] = [
        fmt_dt(queued_at),
        new_run_id,
        new_trigger,
        job_id,
        expected_status,
        expected_run_id,
    ]
    stale_clause = ""
    if stale_before is not None:
        stale_clause = " AND (updated_at IS NULL OR updated_at <= ?)"
        params.append(fmt_dt(stale_before))
    config_clause = ""
    if expected_hash is not None:
        config_clause = " AND config_hash = ?"
        params.append(expected_hash)
    schedule_clause = " AND schedule_enabled = 1" if require_schedule_enabled else ""
    cursor = await db.execute(
        """UPDATE jobs
           SET status = 'queued', updated_at = ?, current_run_id = ?,
               current_trigger = ?
           WHERE job_id = ?
             AND status = ?
             AND current_run_id IS ?"""
        + stale_clause
        + config_clause
        + schedule_clause,
        params,
    )
    if cursor.rowcount != 1:
        await db.rollback()
        return False
    await check_admission(db)
    if expected_hash is not None and prepared_snapshot is not None:
        config_hash = expected_hash
        snapshot_ciphertext = prepared_snapshot
    else:
        row = await (
            await db.execute(
                "SELECT config_yaml, config_hash FROM jobs WHERE job_id = ?",
                (job_id,),
            )
        ).fetchone()
        if row is None:  # pragma: no cover - transaction invariant
            raise RuntimeError("Queued parent disappeared inside its transaction")
        config_hash = cast(str, row["config_hash"])
        config_yaml = reveal_verified(
            cast(str, row["config_yaml"]),
            purpose=f"jobs.config_yaml:{job_id}",
            expected_hash=config_hash,
        )
        if config_yaml is None:
            raise ValueError("Queued parent configuration hash mismatch")
        snapshot_ciphertext = protect_text(
            config_yaml,
            purpose=f"queued_run_snapshots.config_yaml:{new_run_id}",
        )
    await db.execute(
        """INSERT INTO queued_run_snapshots
               (run_id, job_id, trigger, config_hash, config_yaml, queued_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
        (
            new_run_id,
            job_id,
            new_trigger,
            config_hash,
            snapshot_ciphertext,
            fmt_dt(queued_at),
        ),
    )
    return True


async def fail_queued_run(
    db: aiosqlite.Connection,
    job_id: str,
    run_id: str,
    failed_at: datetime,
) -> bool:
    """Fail an enqueue attempt only while its delivery is still current."""

    cursor = await db.execute(
        """UPDATE jobs
           SET status = 'failed', updated_at = ?
           WHERE job_id = ?
             AND status = 'queued'
             AND current_run_id = ?""",
        (fmt_dt(failed_at), job_id, run_id),
    )
    if cursor.rowcount != 1:
        await db.rollback()
        return False
    await job_recovery.insert_failed_queued_run(
        db,
        job_id=job_id,
        run_id=run_id,
        failed_at=failed_at,
        error_count=1,
        failure_code="enqueue_failure",
    )
    return True


async def rollback_queued_submission(
    db: aiosqlite.Connection,
    job_id: str,
    run_id: str,
) -> bool:
    """Remove only a queued ad-hoc row whose Redis enqueue never succeeded."""

    cursor = await db.execute(
        """SELECT 1 FROM jobs
           WHERE job_id = ? AND status = 'queued' AND current_run_id = ?
             AND NOT EXISTS (
                 SELECT 1 FROM job_runs WHERE job_runs.job_id = jobs.job_id
             )
             AND NOT EXISTS (
                 SELECT 1 FROM webhook_deliveries
                 WHERE webhook_deliveries.job_id = jobs.job_id
             )""",
        (job_id, run_id),
    )
    if await cursor.fetchone() is None:
        await db.rollback()
        return False
    await db.execute(
        "DELETE FROM scrape_idempotency WHERE job_id = ? AND run_id = ?",
        (job_id, run_id),
    )
    await db.execute(
        "DELETE FROM queued_run_snapshots WHERE job_id = ? AND run_id = ?",
        (job_id, run_id),
    )
    cursor = await db.execute(
        """DELETE FROM jobs
           WHERE job_id = ? AND status = 'queued' AND current_run_id = ?""",
        (job_id, run_id),
    )
    if cursor.rowcount != 1:  # pragma: no cover - transaction invariant
        raise RuntimeError("Queued submission rollback lost ownership")
    return True
