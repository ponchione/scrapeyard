"""Terminal webhook intents and their reconciliation for the SQLite job store.

Each function runs on a jobs.db connection, and inside any transaction, that
:class:`~scrapeyard.storage.job_store.SQLiteJobStore` opens for it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import cast

import aiosqlite

from scrapeyard.common.dt import fmt_dt, parse_dt
from scrapeyard.common.qualification import qualification_checkpoint
from scrapeyard.models.job import JobStatus
from scrapeyard.storage.secret_envelope import reveal_text
from scrapeyard.storage.types import (
    TerminalIntentAction,
    TerminalIntentReconcileResult,
    TerminalWebhookCandidate,
)
from scrapeyard.storage.webhook_outbox import (
    WebhookDeliveryCreate,
    insert_webhook_delivery,
)

TERMINAL_RUN_STATUSES = {
    JobStatus.complete.value,
    JobStatus.partial.value,
    JobStatus.failed.value,
}
# This code moved out of job_store.py; its log lines keep that logger's name.
logger = logging.getLogger("scrapeyard.storage.job_store")


def validate_terminal_delivery(
    delivery: WebhookDeliveryCreate,
    *,
    job_id: str,
    run_id: str,
    status: str,
) -> None:
    event = f"job.{status}"
    if (
        delivery.job_id != job_id
        or delivery.run_id != run_id
        or delivery.event != event
        or delivery.payload.get("job_id") != job_id
        or delivery.payload.get("run_id") != run_id
        or delivery.payload.get("event") != event
        or delivery.payload.get("status") != status
    ):
        raise ValueError("Terminal webhook delivery does not match the owned job/run/status")


async def commit_terminal_intent(
    db: aiosqlite.Connection,
    delivery: WebhookDeliveryCreate | None,
    *,
    job_id: str,
    run_id: str,
    status: str,
    created_at: datetime,
    checkpoint: str | None = None,
) -> None:
    """Insert the optional terminal webhook intent, then commit the transaction."""
    intent_created = False
    if delivery is not None:
        intent_created = await insert_webhook_delivery(db, delivery, created_at=created_at)
        if checkpoint is not None:
            qualification_checkpoint(checkpoint)
    await db.commit()
    if delivery is not None:
        logger.info(
            "Terminal webhook intent persisted atomically "
            "job_id=%s run_id=%s event=%s delivery_id=%s "
            "terminal_status=%s recovery_action=%s",
            job_id,
            run_id,
            delivery.event,
            delivery.delivery_id,
            status,
            ("atomic_intent_created" if intent_created else "existing_intent_noop"),
        )


async def _mark_terminal_webhook_reconciled(
    db: aiosqlite.Connection,
    candidate: TerminalWebhookCandidate,
) -> None:
    reconciled_at = candidate.completed_at or candidate.heartbeat_at
    cursor = await db.execute(
        """UPDATE job_runs
               SET webhook_reconciled_at = COALESCE(webhook_reconciled_at, ?)
               WHERE job_id = ?
                 AND run_id = ?
                 AND status = ?
                 AND config_hash = ?""",
        (
            fmt_dt(reconciled_at),
            candidate.job_id,
            candidate.run_id,
            candidate.status.value,
            candidate.config_hash,
        ),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("Terminal webhook reconciliation lost run ownership")


async def terminal_webhook_candidate_rows(
    db: aiosqlite.Connection,
    limit: int | None,
) -> list[Mapping[str, object]]:
    # The OR in the WHERE clause keeps SQLite off the partial indexes, so
    # candidate_runs finds each branch's runs through its index first; the
    # WHERE clause still decides which of them are returned.
    query = """SELECT job_runs.job_id,
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
                   FROM (
                       SELECT run_id FROM job_runs
                       WHERE webhook_reconciled_at IS NULL
                         AND status IN ('complete', 'partial', 'failed')
                       UNION
                       SELECT job_runs.run_id FROM jobs
                       JOIN job_runs
                         ON job_runs.job_id = jobs.job_id
                        AND job_runs.run_id = jobs.current_run_id
                       WHERE jobs.status IN ('queued', 'running')
                         AND jobs.current_run_id IS NOT NULL
                         AND jobs.status = 'running'
                   ) AS candidate_runs
                   CROSS JOIN job_runs ON job_runs.run_id = candidate_runs.run_id
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
    parameters: tuple[int, ...] = ()
    if limit is not None:
        if limit < 1:
            raise ValueError("limit must be positive")
        query += " LIMIT ?"
        parameters = (limit,)
    cursor = await db.execute(query, parameters)
    rows = cast(list[Mapping[str, object]], await cursor.fetchall())
    return rows


def terminal_webhook_candidates(
    rows: list[Mapping[str, object]],
) -> list[TerminalWebhookCandidate]:
    """Decrypt each candidate row's config into a :class:`TerminalWebhookCandidate`."""

    candidates: list[TerminalWebhookCandidate] = []
    for row in rows:
        started_at = parse_dt(cast(str | None, row["started_at"]))
        heartbeat_at = parse_dt(cast(str | None, row["heartbeat_at"]))
        if started_at is None or heartbeat_at is None:
            raise ValueError(
                "Terminal run is missing required lifecycle timestamps "
                f"for job_id={row['job_id']!r} run_id={row['run_id']!r}"
            )
        stored_snapshot = cast(str | None, row["run_config_yaml"])
        if stored_snapshot is None:
            config_yaml = reveal_text(
                cast(str, row["parent_config_yaml"]),
                purpose=f"jobs.config_yaml:{cast(str, row['job_id'])}",
            )
        else:
            config_yaml = reveal_text(
                stored_snapshot,
                purpose=f"job_runs.config_yaml:{cast(str, row['run_id'])}",
            )
        candidates.append(
            TerminalWebhookCandidate(
                job_id=cast(str, row["job_id"]),
                run_id=cast(str, row["run_id"]),
                status=JobStatus(cast(str, row["status"])),
                project=cast(str, row["project"]),
                name=cast(str, row["name"]),
                config_yaml=config_yaml,
                has_config_snapshot=stored_snapshot is not None,
                config_hash=cast(str, row["config_hash"]),
                started_at=started_at,
                heartbeat_at=heartbeat_at,
                completed_at=parse_dt(cast(str | None, row["completed_at"])),
                record_count=cast(int | None, row["record_count"]),
                error_count=cast(int, row["error_count"]),
                parent_status=JobStatus(cast(str, row["parent_status"])),
                current_run_id=cast(str | None, row["current_run_id"]),
                existing_delivery_id=cast(
                    str | None,
                    row["existing_delivery_id"],
                ),
                existing_delivery_status=cast(
                    str | None,
                    row["existing_delivery_status"],
                ),
                existing_delivery_scrubbed_at=parse_dt(
                    cast(str | None, row["existing_delivery_scrubbed_at"])
                ),
            )
        )
    return candidates


async def reconcile_terminal_webhook_candidate(
    db: aiosqlite.Connection,
    candidate: TerminalWebhookCandidate,
    webhook_delivery: WebhookDeliveryCreate | None,
) -> TerminalIntentReconcileResult:
    cursor = await db.execute(
        """SELECT job_runs.status AS run_status,
                              job_runs.config_hash,
                              job_runs.config_yaml AS run_config_yaml,
                              jobs.status AS parent_status,
                              jobs.current_run_id,
                              jobs.config_yaml AS parent_config_yaml
                       FROM job_runs
                       JOIN jobs ON jobs.job_id = job_runs.job_id
                       WHERE job_runs.job_id = ? AND job_runs.run_id = ?""",
        (candidate.job_id, candidate.run_id),
    )
    row = await cursor.fetchone()
    if (
        row is None
        or row["run_status"] != candidate.status.value
        or row["run_status"] not in TERMINAL_RUN_STATUSES
        or row["config_hash"] != candidate.config_hash
        or row["parent_status"] == JobStatus.deleting.value
    ):
        await db.rollback()
        return TerminalIntentReconcileResult(TerminalIntentAction.race_noop)

    stored_snapshot = cast(str | None, row["run_config_yaml"])
    if candidate.has_config_snapshot:
        config_matches = stored_snapshot is not None and reveal_text(
            stored_snapshot,
            purpose=f"job_runs.config_yaml:{candidate.run_id}",
        ) == candidate.config_yaml
    else:
        config_matches = stored_snapshot is None and reveal_text(
            cast(str, row["parent_config_yaml"]),
            purpose=f"jobs.config_yaml:{candidate.job_id}",
        ) == candidate.config_yaml
    if not config_matches:
        await db.rollback()
        return TerminalIntentReconcileResult(TerminalIntentAction.race_noop)

    parent_converged = False
    if (
        row["current_run_id"] == candidate.run_id
        and row["parent_status"] == JobStatus.running.value
    ):
        cursor = await db.execute(
            """UPDATE jobs
                           SET status = ?, updated_at = ?
                           WHERE job_id = ?
                             AND status = 'running'
                             AND current_run_id = ?""",
            (
                candidate.status.value,
                fmt_dt(candidate.completed_at or candidate.heartbeat_at),
                candidate.job_id,
                candidate.run_id,
            ),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            return TerminalIntentReconcileResult(TerminalIntentAction.race_noop)
        parent_converged = True

    event = f"job.{candidate.status.value}"
    cursor = await db.execute(
        """SELECT delivery_id
                       FROM webhook_deliveries
                       WHERE job_id = ? AND run_id = ? AND event = ?
                       ORDER BY created_at ASC, delivery_id ASC
                       LIMIT 1""",
        (candidate.job_id, candidate.run_id, event),
    )
    existing = await cursor.fetchone()
    if existing is not None:
        await _mark_terminal_webhook_reconciled(db, candidate)
        await db.commit()
        return TerminalIntentReconcileResult(
            TerminalIntentAction.existing,
            parent_converged=parent_converged,
            delivery_id=str(existing["delivery_id"]),
        )

    if webhook_delivery is None:
        await _mark_terminal_webhook_reconciled(db, candidate)
        await db.commit()
        return TerminalIntentReconcileResult(
            TerminalIntentAction.not_required,
            parent_converged=parent_converged,
        )

    validate_terminal_delivery(
        webhook_delivery,
        job_id=candidate.job_id,
        run_id=candidate.run_id,
        status=candidate.status.value,
    )
    created = await insert_webhook_delivery(
        db,
        webhook_delivery,
        created_at=candidate.completed_at or candidate.heartbeat_at,
    )
    if not created:
        cursor = await db.execute(
            """SELECT job_id, run_id, event
                           FROM webhook_deliveries
                           WHERE delivery_id = ?""",
            (webhook_delivery.delivery_id,),
        )
        collision = await cursor.fetchone()
        if (
            collision is None
            or collision["job_id"] != candidate.job_id
            or collision["run_id"] != candidate.run_id
            or collision["event"] != event
        ):
            raise RuntimeError(
                "Deterministic webhook delivery ID collided with another event"
            )
    await _mark_terminal_webhook_reconciled(db, candidate)
    await db.commit()
    return TerminalIntentReconcileResult(
        (TerminalIntentAction.created if created else TerminalIntentAction.existing),
        parent_converged=parent_converged,
        delivery_id=webhook_delivery.delivery_id,
    )


async def mark_terminal_webhook_reconciliation_failure(
    db: aiosqlite.Connection,
    job_id: str,
    run_id: str,
    failed_at: datetime,
) -> bool:
    cursor = await db.execute(
        """UPDATE job_runs
               SET webhook_reconciliation_failed_at = ?
               WHERE job_id = ? AND run_id = ?
                 AND status IN ('complete', 'partial', 'failed')
                 AND webhook_reconciled_at IS NULL""",
        (fmt_dt(failed_at), job_id, run_id),
    )
    return cursor.rowcount == 1
