"""Job cancellation and deletion for the SQLite job store.

Each function runs on a jobs.db connection, and inside any transaction, that
:class:`~scrapeyard.storage.job_store.SQLiteJobStore` opens for it.
"""

from __future__ import annotations

from datetime import datetime
from typing import cast

import aiosqlite

from scrapeyard.common.dt import fmt_dt
from scrapeyard.models.job import JobStatus
from scrapeyard.storage.types import (
    CancellationAction,
    CancellationOutcome,
    DeletionFinalizationAction,
    DeletionFinalizationOutcome,
    DeletionReservationAction,
    DeletionReservationOutcome,
)


async def cancel_job(
    db: aiosqlite.Connection,
    job_id: str,
    cancelled_at: datetime,
    *,
    expected_run_id: str | None,
) -> CancellationOutcome:
    cancelled_text = fmt_dt(cancelled_at)
    cursor = await db.execute(
        "SELECT status, current_run_id FROM jobs WHERE job_id = ?",
        (job_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        await db.rollback()
        return CancellationOutcome(
            CancellationAction.missing,
            job_id,
            None,
            None,
            None,
        )

    prior_status = JobStatus(str(row["status"]))
    run_id = cast(str | None, row["current_run_id"])
    if expected_run_id is not None and run_id != expected_run_id:
        await db.rollback()
        return CancellationOutcome(
            CancellationAction.run_conflict, job_id, run_id, prior_status, prior_status,
        )
    if prior_status is JobStatus.cancelled:
        await db.rollback()
        return CancellationOutcome(
            CancellationAction.already_cancelled,
            job_id,
            run_id,
            prior_status,
            JobStatus.cancelled,
        )
    if prior_status not in {JobStatus.queued, JobStatus.running}:
        await db.rollback()
        return CancellationOutcome(
            CancellationAction.conflict,
            job_id,
            run_id,
            prior_status,
            prior_status,
        )

    if prior_status is JobStatus.running:
        if run_id is None:
            raise RuntimeError("Running job cannot be cancelled without current_run_id")
        cursor = await db.execute(
            """UPDATE job_runs
                       SET status = 'cancelled', completed_at = ?
                       WHERE job_id = ?
                         AND run_id = ?
                         AND status = 'running'""",
            (cancelled_text, job_id, run_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Running job cancellation could not update its exact run")
    # Keep an unstarted delivery's immutable snapshot for exact-run recovery.
    # Job deletion/retention cascades it; it does not become a fake execution.

    cursor = await db.execute(
        """UPDATE jobs
                   SET status = 'cancelled',
                       schedule_enabled = 0,
                       updated_at = ?
                   WHERE job_id = ?
                     AND status = ?
                     AND current_run_id IS ?""",
        (cancelled_text, job_id, prior_status.value, run_id),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("Job cancellation lost ownership inside its transaction")
    return CancellationOutcome(
        CancellationAction.cancelled,
        job_id,
        run_id,
        prior_status,
        JobStatus.cancelled,
    )


async def reserve_job_deletion(
    db: aiosqlite.Connection,
    job_id: str,
    *,
    delete_results: bool,
    requested_at: datetime,
) -> DeletionReservationOutcome:
    requested_text = fmt_dt(requested_at)
    cursor = await db.execute(
        """SELECT status, current_run_id, delete_results_on_delete
                       FROM jobs WHERE job_id = ?""",
        (job_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        await db.rollback()
        return DeletionReservationOutcome(
            DeletionReservationAction.missing,
            job_id,
            None,
            None,
            delete_results,
        )

    prior_status = JobStatus(str(row["status"]))
    run_id = cast(str | None, row["current_run_id"])
    stored_policy = row["delete_results_on_delete"]
    if prior_status in {JobStatus.queued, JobStatus.running}:
        await db.rollback()
        return DeletionReservationOutcome(
            DeletionReservationAction.active_conflict,
            job_id,
            run_id,
            prior_status,
            delete_results,
        )
    if prior_status is JobStatus.deleting:
        if stored_policy is None or bool(stored_policy) != delete_results:
            await db.rollback()
            return DeletionReservationOutcome(
                DeletionReservationAction.policy_conflict,
                job_id,
                run_id,
                prior_status,
                delete_results,
            )
        action = DeletionReservationAction.resumed
    elif prior_status in {
        JobStatus.cancelled,
        JobStatus.complete,
        JobStatus.partial,
        JobStatus.failed,
    }:
        action = DeletionReservationAction.created
    else:  # pragma: no cover - exhaustive enum defense
        raise RuntimeError(f"Unsupported deletion source status: {prior_status.value}")

    cursor = await db.execute(
        """SELECT 1 FROM webhook_deliveries
                       WHERE job_id = ? AND status = 'pending'
                       LIMIT 1""",
        (job_id,),
    )
    if await cursor.fetchone() is not None:
        await db.rollback()
        return DeletionReservationOutcome(
            DeletionReservationAction.pending_webhook_conflict,
            job_id,
            run_id,
            prior_status,
            delete_results,
        )

    if action is DeletionReservationAction.created:
        cursor = await db.execute(
            """UPDATE jobs
                           SET status = 'deleting',
                               schedule_enabled = 0,
                               updated_at = ?,
                               deletion_requested_at = ?,
                               delete_results_on_delete = ?
                           WHERE job_id = ?
                             AND status = ?
                             AND current_run_id IS ?""",
            (
                requested_text,
                requested_text,
                int(delete_results),
                job_id,
                prior_status.value,
                run_id,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Deletion reservation lost ownership inside transaction")
    cursor = await db.execute(
        "SELECT run_id FROM job_runs WHERE job_id = ? ORDER BY run_id",
        (job_id,),
    )
    owned_run_ids = {str(item["run_id"]) for item in await cursor.fetchall()}
    if run_id is not None:
        owned_run_ids.add(run_id)
    return DeletionReservationOutcome(
        action,
        job_id,
        run_id,
        prior_status,
        delete_results,
        tuple(sorted(owned_run_ids)),
    )


async def finalize_job_deletion(
    db: aiosqlite.Connection,
    job_id: str,
    *,
    delete_results: bool,
    preserve_idempotency_after: datetime | None,
) -> DeletionFinalizationOutcome:
    cursor = await db.execute(
        "SELECT status, delete_results_on_delete FROM jobs WHERE job_id = ?",
        (job_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        await db.rollback()
        return DeletionFinalizationOutcome(
            DeletionFinalizationAction.missing,
            job_id,
        )
    if row["status"] != JobStatus.deleting.value:
        await db.rollback()
        return DeletionFinalizationOutcome(
            DeletionFinalizationAction.not_reserved,
            job_id,
        )
    if (
        row["delete_results_on_delete"] is None
        or bool(row["delete_results_on_delete"]) != delete_results
    ):
        await db.rollback()
        return DeletionFinalizationOutcome(
            DeletionFinalizationAction.policy_conflict,
            job_id,
        )
    cursor = await db.execute(
        """SELECT 1 FROM webhook_deliveries
                       WHERE job_id = ? AND status = 'pending'
                       LIMIT 1""",
        (job_id,),
    )
    if await cursor.fetchone() is not None:
        await db.rollback()
        return DeletionFinalizationOutcome(
            DeletionFinalizationAction.pending_webhook_conflict,
            job_id,
        )
    if preserve_idempotency_after is not None:
        cursor = await db.execute(
            """SELECT 1 FROM scrape_idempotency
                           WHERE job_id = ? AND expires_at > ?
                           LIMIT 1""",
            (job_id, fmt_dt(preserve_idempotency_after)),
        )
        if await cursor.fetchone() is not None:
            await db.rollback()
            return DeletionFinalizationOutcome(
                DeletionFinalizationAction.unexpired_idempotency_conflict,
                job_id,
            )

    await db.execute(
        "DELETE FROM webhook_deliveries WHERE job_id = ?",
        (job_id,),
    )
    await db.execute(
        "DELETE FROM scrape_idempotency WHERE job_id = ?",
        (job_id,),
    )
    await db.execute(
        "DELETE FROM queued_run_snapshots WHERE job_id = ?",
        (job_id,),
    )
    await db.execute("DELETE FROM job_runs WHERE job_id = ?", (job_id,))
    cursor = await db.execute(
        """DELETE FROM jobs
                       WHERE job_id = ?
                         AND status = 'deleting'
                         AND delete_results_on_delete = ?""",
        (job_id, int(delete_results)),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("Final job deletion lost its persisted reservation")
    return DeletionFinalizationOutcome(
        DeletionFinalizationAction.deleted,
        job_id,
    )
