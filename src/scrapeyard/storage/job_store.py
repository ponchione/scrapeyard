"""SQLite-backed implementation of the JobStore protocol."""

from __future__ import annotations

import logging
import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import NoReturn, cast

import aiosqlite

from scrapeyard.common.dt import fmt_dt, parse_dt
from scrapeyard.common.qualification import qualification_checkpoint
from scrapeyard.models.job import Job, JobRun, JobStatus
from scrapeyard.storage.database import db_transaction, get_db
from scrapeyard.storage.job_queries import (
    PROJECT_SUMMARY_QUERY,
    SCHEDULED_JOBS_QUERY,
    build_list_jobs_with_stats_query,
)
from scrapeyard.storage.job_rows import (
    row_to_job,
    row_to_job_run,
    row_to_job_with_stats,
    row_to_project_summary,
    row_to_schedule_state,
)
from scrapeyard.storage.job_sql import JOB_COLUMNS, JOB_RUN_COLUMNS, select_columns
from scrapeyard.storage.types import (
    CancellationAction,
    CancellationOutcome,
    CleanupBacklogSnapshot,
    DeletionFinalizationAction,
    DeletionFinalizationOutcome,
    DeletionReservationAction,
    DeletionReservationOutcome,
    HistoryPruneResult,
    IdempotentJobAction,
    IdempotentJobOutcome,
    RunOwnershipError,
    RunRecovery,
    ScheduledJobMutationAction,
    ScheduledJobMutationOutcome,
    StaleQueuedJob,
    TerminalIntentAction,
    TerminalIntentReconcileResult,
    TerminalWebhookCandidate,
)
from scrapeyard.storage.webhook_outbox import (
    WebhookDeliveryCreate,
    insert_webhook_delivery,
)
from scrapeyard.storage.secret_envelope import (
    SecretDecryptionError,
    protect_text,
    reveal_text,
)


_TERMINAL_RUN_STATUSES = {
    JobStatus.complete.value,
    JobStatus.partial.value,
    JobStatus.failed.value,
}
logger = logging.getLogger(__name__)
_SCHEDULE_FAILURE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class DuplicateJobError(Exception):
    """Raised when a job name already exists within a project namespace."""

    def __init__(self, project: str, name: str) -> None:
        self.project = project
        self.name = name
        super().__init__(f"Job {name!r} already exists in project {project!r}")


def is_duplicate_job_integrity_error(error: str | Exception) -> bool:
    message = error if isinstance(error, str) else str(error)
    return (
        "UNIQUE constraint failed: jobs.project, jobs.name" in message
        or "jobs.project, jobs.name" in message
    )


class SQLiteJobStore:
    """SQLite implementation of :class:`~scrapeyard.storage.protocols.JobStore`."""

    async def _execute_write(
        self,
        sql: str,
        params: Sequence[object] = (),
    ) -> aiosqlite.Cursor:
        async with get_db("jobs.db") as db, db_transaction(db):
            return await db.execute(sql, params)

    @staticmethod
    async def _insert_job(db: aiosqlite.Connection, job: Job) -> None:
        current_trigger = job.current_trigger
        if current_trigger is None and job.current_run_id is not None:
            current_trigger = "scheduled" if job.schedule_cron is not None else "adhoc"
        protected_config = protect_text(
            job.config_yaml,
            purpose=f"jobs.config_yaml:{job.job_id}",
        )
        await db.execute(
            """INSERT INTO jobs (job_id, project, name, status,
               config_yaml, config_hash, created_at, updated_at, schedule_cron,
               schedule_timezone, schedule_enabled, current_run_id, current_trigger,
               deletion_requested_at, delete_results_on_delete,
               schedule_failure_at, schedule_failure_code,
               schedule_consecutive_failures)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job.job_id,
                job.project,
                job.name,
                job.status.value,
                protected_config,
                hashlib.sha256(job.config_yaml.encode("utf-8")).hexdigest(),
                fmt_dt(job.created_at),
                fmt_dt(job.updated_at),
                job.schedule_cron,
                job.schedule_timezone,
                int(job.schedule_enabled),
                job.current_run_id,
                current_trigger,
                fmt_dt(job.deletion_requested_at),
                (
                    None
                    if job.delete_results_on_delete is None
                    else int(job.delete_results_on_delete)
                ),
                fmt_dt(job.schedule_failure_at),
                job.schedule_failure_code,
                job.schedule_consecutive_failures,
            ),
        )
        if job.status is JobStatus.queued and job.current_run_id is not None:
            trigger = current_trigger or "adhoc"
            queued_at = job.updated_at or job.created_at
            await db.execute(
                """INSERT INTO queued_run_snapshots
                       (run_id, job_id, trigger, config_hash, config_yaml, queued_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    job.current_run_id,
                    job.job_id,
                    trigger,
                    hashlib.sha256(job.config_yaml.encode("utf-8")).hexdigest(),
                    protect_text(
                        job.config_yaml,
                        purpose=(
                            "queued_run_snapshots.config_yaml:"
                            f"{job.current_run_id}"
                        ),
                    ),
                    fmt_dt(queued_at),
                ),
            )

    @staticmethod
    async def _get_job_in_db(
        db: aiosqlite.Connection,
        job_id: str,
    ) -> Job | None:
        cursor = await db.execute(
            f"SELECT {select_columns(JOB_COLUMNS)} FROM jobs WHERE job_id = ?",
            (job_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else row_to_job(cast(Mapping[str, object], row))

    @staticmethod
    def _raise_ownership(operation: str, job_id: str, run_id: str) -> NoReturn:
        raise RunOwnershipError(operation, job_id, run_id)

    @staticmethod
    def _validate_terminal_delivery(
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

    @staticmethod
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

    async def save_job(self, job: Job) -> str:
        async with get_db("jobs.db") as db, db_transaction(db):
            try:
                await self._insert_job(db, job)
            except aiosqlite.IntegrityError as exc:
                if is_duplicate_job_integrity_error(exc):
                    raise DuplicateJobError(job.project, job.name) from exc
                raise
        return job.job_id

    async def create_idempotent_job(
        self,
        job: Job,
        *,
        caller_scope: str,
        key_digest: str,
        request_hash: str,
        response_mode: str,
        expires_at: datetime,
    ) -> IdempotentJobOutcome:
        """Create one job/idempotency pair or return the serialized winner."""

        if job.current_run_id is None:
            raise ValueError("Idempotent ad-hoc jobs require current_run_id")
        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
            # Lazy expiry makes a key reusable on its first request after the
            # retention window, independently of the periodic cleanup cadence.
            await db.execute(
                """DELETE FROM scrape_idempotency
                   WHERE caller_scope = ? AND key_digest = ? AND expires_at <= ?""",
                (caller_scope, key_digest, fmt_dt(job.created_at)),
            )
            cursor = await db.execute(
                f"""SELECT scrape_idempotency.request_hash,
                           scrape_idempotency.run_id,
                           scrape_idempotency.response_mode,
                           {select_columns(JOB_COLUMNS, table_alias='jobs')}
                    FROM scrape_idempotency
                    JOIN jobs ON jobs.job_id = scrape_idempotency.job_id
                    WHERE scrape_idempotency.caller_scope = ?
                      AND scrape_idempotency.key_digest = ?""",
                (caller_scope, key_digest),
            )
            row = await cursor.fetchone()
            if row is not None:
                existing = row_to_job(cast(Mapping[str, object], row))
                action = (
                    IdempotentJobAction.matched
                    if row["request_hash"] == request_hash
                    and row["response_mode"] == response_mode
                    else IdempotentJobAction.conflict
                )
                return IdempotentJobOutcome(
                    action=action,
                    job=existing,
                    run_id=cast(str, row["run_id"]),
                    response_mode=cast(str, row["response_mode"]),
                )

            try:
                await self._insert_job(db, job)
            except aiosqlite.IntegrityError as exc:
                if is_duplicate_job_integrity_error(exc):
                    raise DuplicateJobError(job.project, job.name) from exc
                raise
            await db.execute(
                """INSERT INTO scrape_idempotency
                   (caller_scope, key_digest, request_hash, job_id, run_id,
                    response_mode, created_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    caller_scope,
                    key_digest,
                    request_hash,
                    job.job_id,
                    job.current_run_id,
                    response_mode,
                    fmt_dt(job.created_at),
                    fmt_dt(expires_at),
                ),
            )
            return IdempotentJobOutcome(
                action=IdempotentJobAction.created,
                job=job,
                run_id=job.current_run_id,
                response_mode=response_mode,
            )

    async def delete_expired_idempotency_records(
        self,
        expired_before: datetime,
        *,
        limit: int,
    ) -> int:
        """Delete a deterministic bounded batch of expired records."""

        if limit < 1:
            raise ValueError("Idempotency cleanup limit must be positive")
        cursor = await self._execute_write(
            """DELETE FROM scrape_idempotency
               WHERE rowid IN (
                   SELECT rowid FROM scrape_idempotency
                   WHERE expires_at <= ?
                   ORDER BY expires_at, caller_scope, key_digest
                   LIMIT ?
               )""",
            (fmt_dt(expired_before), limit),
        )
        return cursor.rowcount

    async def summarize_cleanup_backlog(
        self,
        *,
        observed_at: datetime,
        adhoc_expired_before: datetime,
        scheduled_expired_before: datetime,
        tombstone_expired_before: datetime,
        max_scheduled_runs_per_job: int,
    ) -> dict[str, CleanupBacklogSnapshot]:
        """Return exact job/idempotency backlog using retention predicates."""

        async with get_db("jobs.db") as db:
            idempotency_cursor = await db.execute(
                """SELECT COUNT(*) AS eligible_count,
                          MIN(expires_at) AS oldest_eligible_at
                   FROM scrape_idempotency
                   WHERE expires_at <= ?""",
                (fmt_dt(observed_at),),
            )
            idempotency_row = await idempotency_cursor.fetchone()
            adhoc_cursor = await db.execute(
                """SELECT COUNT(*) AS eligible_count,
                          MIN(COALESCE(
                              jobs.deletion_requested_at,
                              jobs.updated_at,
                              jobs.created_at
                          )) AS oldest_eligible_at
                   FROM jobs
                   WHERE jobs.schedule_cron IS NULL
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
                     )""",
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
                "FROM job_runs JOIN jobs ON jobs.job_id = job_runs.job_id WHERE "
                + self._scheduled_history_eligibility_sql(),
                (
                    fmt_dt(tombstone_expired_before),
                    fmt_dt(scheduled_expired_before),
                    max_scheduled_runs_per_job,
                ),
            )
            scheduled_row = await scheduled_cursor.fetchone()
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
        self,
        expired_before: datetime,
        *,
        idempotency_observed_at: datetime,
        tombstone_expired_before: datetime,
        limit: int,
    ) -> list[str]:
        """Return aged ad-hoc jobs whose delivery history is safe to compact."""

        if limit < 1:
            raise ValueError("Ad-hoc job cleanup limit must be positive")
        async with get_db("jobs.db") as db:
            cursor = await db.execute(
                """SELECT jobs.job_id
                   FROM jobs
                   WHERE jobs.schedule_cron IS NULL
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

    @staticmethod
    def _scheduled_history_eligibility_sql() -> str:
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
        self,
        expired_before: datetime,
        *,
        tombstone_expired_before: datetime,
        max_runs_per_job: int,
        limit: int,
    ) -> list[tuple[str, str]]:
        """Return bounded scheduled run candidates after durable reconciliation."""

        if max_runs_per_job < 1 or limit < 1:
            raise ValueError("Scheduled run retention bounds must be positive")
        query = (
            "SELECT job_runs.job_id, job_runs.run_id "
            "FROM job_runs JOIN jobs ON jobs.job_id = job_runs.job_id WHERE "
            + self._scheduled_history_eligibility_sql()
            + " ORDER BY COALESCE(job_runs.completed_at, job_runs.started_at), "
            "job_runs.job_id, job_runs.run_id LIMIT ?"
        )
        async with get_db("jobs.db") as db:
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
        self,
        job_id: str,
        run_id: str,
        *,
        expired_before: datetime,
        tombstone_expired_before: datetime,
        max_runs_per_job: int,
    ) -> HistoryPruneResult:
        """Atomically recheck and compact one run and its terminal tombstones."""

        if max_runs_per_job < 1:
            raise ValueError("Scheduled run retention count must be positive")
        query = (
            "SELECT 1 FROM job_runs "
            "JOIN jobs ON jobs.job_id = job_runs.job_id "
            "WHERE job_runs.job_id = ? AND job_runs.run_id = ? AND "
            + self._scheduled_history_eligibility_sql()
        )
        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
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

    async def update_scheduled_job(
        self,
        job_id: str,
        *,
        project: str,
        name: str,
        config_yaml: str,
        schedule_cron: str,
        schedule_timezone: str,
        schedule_enabled: bool,
        updated_at: datetime,
    ) -> ScheduledJobMutationOutcome:
        """Replace future scheduled config while excluding an accepted run."""

        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
            previous = await self._get_job_in_db(db, job_id)
            if previous is None:
                return ScheduledJobMutationOutcome(ScheduledJobMutationAction.missing)
            if previous.schedule_cron is None:
                return ScheduledJobMutationOutcome(
                    ScheduledJobMutationAction.not_scheduled,
                    previous,
                    previous,
                )
            if previous.project != project:
                return ScheduledJobMutationOutcome(
                    ScheduledJobMutationAction.project_conflict,
                    previous,
                    previous,
                )
            if previous.status in {JobStatus.cancelled, JobStatus.deleting}:
                return ScheduledJobMutationOutcome(
                    ScheduledJobMutationAction.lifecycle_conflict,
                    previous,
                    previous,
                )
            if previous.status is JobStatus.running or (
                previous.status is JobStatus.queued
                and previous.current_run_id is not None
            ):
                return ScheduledJobMutationOutcome(
                    ScheduledJobMutationAction.active_conflict,
                    previous,
                    previous,
                )
            try:
                protected_config = protect_text(
                    config_yaml,
                    purpose=f"jobs.config_yaml:{job_id}",
                )
                await db.execute(
                    """UPDATE jobs
                       SET project = ?, name = ?, config_yaml = ?, config_hash = ?,
                           schedule_cron = ?, schedule_timezone = ?,
                           schedule_enabled = ?, updated_at = ?
                       WHERE job_id = ?""",
                    (
                        project,
                        name,
                        protected_config,
                        hashlib.sha256(config_yaml.encode("utf-8")).hexdigest(),
                        schedule_cron,
                        schedule_timezone,
                        int(schedule_enabled),
                        fmt_dt(updated_at),
                        job_id,
                    ),
                )
            except aiosqlite.IntegrityError as exc:
                if is_duplicate_job_integrity_error(exc):
                    raise DuplicateJobError(project, name) from exc
                raise
            current = await self._get_job_in_db(db, job_id)
            if current is None:  # pragma: no cover - transaction invariant
                raise RuntimeError("Scheduled job disappeared during update")
            return ScheduledJobMutationOutcome(
                ScheduledJobMutationAction.updated,
                previous,
                current,
            )

    async def set_schedule_enabled(
        self,
        job_id: str,
        *,
        enabled: bool,
        updated_at: datetime,
    ) -> ScheduledJobMutationOutcome:
        """Persist pause/resume state independently of current run execution."""

        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
            previous = await self._get_job_in_db(db, job_id)
            if previous is None:
                return ScheduledJobMutationOutcome(ScheduledJobMutationAction.missing)
            if previous.schedule_cron is None:
                return ScheduledJobMutationOutcome(
                    ScheduledJobMutationAction.not_scheduled,
                    previous,
                    previous,
                )
            if previous.status in {JobStatus.cancelled, JobStatus.deleting}:
                return ScheduledJobMutationOutcome(
                    ScheduledJobMutationAction.lifecycle_conflict,
                    previous,
                    previous,
                )
            if previous.schedule_enabled is enabled:
                return ScheduledJobMutationOutcome(
                    ScheduledJobMutationAction.unchanged,
                    previous,
                    previous,
                )
            await db.execute(
                """UPDATE jobs SET schedule_enabled = ?, updated_at = ?
                   WHERE job_id = ?""",
                (int(enabled), fmt_dt(updated_at), job_id),
            )
            current = previous.model_copy(
                update={"schedule_enabled": enabled, "updated_at": updated_at}
            )
            return ScheduledJobMutationOutcome(
                ScheduledJobMutationAction.updated,
                previous,
                current,
            )

    async def restore_scheduled_job(self, expected: Job, previous: Job) -> bool:
        """Restore one exact just-written snapshot after local scheduler failure."""

        cursor = await self._execute_write(
            """UPDATE jobs
               SET project = ?, name = ?, config_yaml = ?, config_hash = ?, schedule_cron = ?,
                   schedule_timezone = ?, schedule_enabled = ?, updated_at = ?
               WHERE job_id = ?
                 AND project = ? AND name = ? AND config_hash = ?
                 AND schedule_cron IS ? AND schedule_timezone = ?
                 AND schedule_enabled = ? AND updated_at IS ?
                 AND status = ? AND current_run_id IS ?""",
            (
                previous.project,
                previous.name,
                protect_text(
                    previous.config_yaml,
                    purpose=f"jobs.config_yaml:{previous.job_id}",
                ),
                hashlib.sha256(previous.config_yaml.encode("utf-8")).hexdigest(),
                previous.schedule_cron,
                previous.schedule_timezone,
                int(previous.schedule_enabled),
                fmt_dt(previous.updated_at),
                expected.job_id,
                expected.project,
                expected.name,
                hashlib.sha256(expected.config_yaml.encode("utf-8")).hexdigest(),
                expected.schedule_cron,
                expected.schedule_timezone,
                int(expected.schedule_enabled),
                fmt_dt(expected.updated_at),
                expected.status.value,
                expected.current_run_id,
            ),
        )
        return cursor.rowcount == 1

    async def rollback_scheduled_job_creation(self, job_id: str) -> bool:
        """Remove a scheduled row only before any run or delivery exists."""

        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
            cursor = await db.execute(
                """DELETE FROM jobs
                   WHERE job_id = ?
                     AND schedule_cron IS NOT NULL
                     AND status = 'queued'
                     AND current_run_id IS NULL
                     AND NOT EXISTS (
                         SELECT 1 FROM job_runs WHERE job_runs.job_id = jobs.job_id
                     )
                     AND NOT EXISTS (
                         SELECT 1 FROM webhook_deliveries
                         WHERE webhook_deliveries.job_id = jobs.job_id
                     )""",
                (job_id,),
            )
            return cursor.rowcount == 1

    async def get_job(self, job_id: str) -> Job:
        async with get_db("jobs.db") as db:
            cursor = await db.execute(
                f"SELECT {select_columns(JOB_COLUMNS)} FROM jobs WHERE job_id=?",
                (job_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            raise KeyError(f"Job not found: {job_id!r}")
        return row_to_job(cast(Mapping[str, object], row))

    async def cancel_job(
        self,
        job_id: str,
        cancelled_at: datetime,
    ) -> CancellationOutcome:
        """Cancel the exact accepted delivery under one immediate transaction."""

        cancelled_text = fmt_dt(cancelled_at)
        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
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
            elif run_id is not None:
                await db.execute(
                    "DELETE FROM queued_run_snapshots WHERE job_id = ? AND run_id = ?",
                    (job_id, run_id),
                )

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

    async def run_is_active(self, job_id: str, run_id: str) -> bool:
        """Check exact parent/run ownership at a cooperative lifecycle boundary."""

        async with get_db("jobs.db") as db:
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
        self,
        project: str,
        job_name: str,
        run_id: str,
    ) -> bool:
        """Protect exact queued/running result ownership during reconciliation."""

        async with get_db("jobs.db") as db:
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

    async def reserve_job_deletion(
        self,
        job_id: str,
        *,
        delete_results: bool,
        requested_at: datetime,
    ) -> DeletionReservationOutcome:
        """Create or resume a deletion reservation without crossing databases."""

        requested_text = fmt_dt(requested_at)
        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
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
            return DeletionReservationOutcome(
                action,
                job_id,
                run_id,
                prior_status,
                delete_results,
            )

    async def finalize_job_deletion(
        self,
        job_id: str,
        *,
        delete_results: bool,
        preserve_idempotency_after: datetime | None = None,
    ) -> DeletionFinalizationOutcome:
        """Delete all jobs.db state only while the reservation still matches."""

        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
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

    async def get_job_runs(self, job_id: str, limit: int = 10) -> list[JobRun]:
        """Return the last N runs for a job, newest first."""
        async with get_db("jobs.db") as db:
            cursor = await db.execute(
                f"SELECT {select_columns(JOB_RUN_COLUMNS)} FROM job_runs WHERE job_id = ? "
                "ORDER BY started_at DESC LIMIT ?",
                (job_id, limit),
            )
            rows = cast(list[Mapping[str, object]], await cursor.fetchall())
            return [row_to_job_run(row) for row in rows]

    async def get_job_run(self, job_id: str, run_id: str) -> JobRun | None:
        """Return one run only when it belongs to the expected parent job."""
        async with get_db("jobs.db") as db:
            cursor = await db.execute(
                f"SELECT {select_columns(JOB_RUN_COLUMNS)} FROM job_runs "
                "WHERE job_id = ? AND run_id = ?",
                (job_id, run_id),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return row_to_job_run(cast(Mapping[str, object], row))

    async def get_job_run_stats(
        self,
        job_id: str,
    ) -> tuple[int, datetime | None]:
        """Return lifetime run count and last start without scanning history."""
        async with get_db("jobs.db") as db:
            cursor = await db.execute(
                "SELECT lifetime_run_count AS run_count, last_run_at "
                "FROM jobs WHERE job_id = ?",
                (job_id,),
            )
            row = await cursor.fetchone()
            count = row["run_count"] if row else 0
            last = (
                datetime.fromisoformat(row["last_run_at"]) if row and row["last_run_at"] else None
            )
            return count, last

    async def list_jobs_with_stats(
        self,
        project: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[tuple[Job, int, datetime | None]]:
        """List jobs with lifetime run summaries, newest activity first."""
        sql, params = build_list_jobs_with_stats_query(project, limit, offset)
        async with get_db("jobs.db") as db:
            cursor = await db.execute(sql, params)
            rows = cast(list[Mapping[str, object]], await cursor.fetchall())
        return [row_to_job_with_stats(row) for row in rows]

    async def summary_by_project(self) -> list[tuple[str, str, int]]:
        """Return grouped job counts by project and status."""
        async with get_db("jobs.db") as db:
            cursor = await db.execute(PROJECT_SUMMARY_QUERY)
            rows = cast(list[Mapping[str, object]], await cursor.fetchall())
        return [row_to_project_summary(row) for row in rows]

    async def get_queued_run_config(self, job_id: str, run_id: str) -> str:
        """Load the encrypted immutable config for one accepted delivery."""

        async with get_db("jobs.db") as db:
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
                config_yaml = reveal_text(
                    cast(str, row["config_yaml"]),
                    purpose=f"queued_run_snapshots.config_yaml:{run_id}",
                )
                if hashlib.sha256(config_yaml.encode("utf-8")).hexdigest() != row["config_hash"]:
                    raise ValueError("Queued run configuration hash mismatch")
                return config_yaml

            # Upgrade compatibility for deliveries accepted before migration
            # 019. New writes always use queued_run_snapshots.
            cursor = await db.execute(
                """SELECT config_yaml, config_hash FROM jobs
                   WHERE job_id = ? AND status = 'queued' AND current_run_id = ?""",
                (job_id, run_id),
            )
            legacy = await cursor.fetchone()
            if legacy is None:
                raise RunOwnershipError("load queued config", job_id, run_id)
            config_yaml = reveal_text(
                cast(str, legacy["config_yaml"]),
                purpose=f"jobs.config_yaml:{job_id}",
            )
            if hashlib.sha256(config_yaml.encode("utf-8")).hexdigest() != legacy["config_hash"]:
                raise ValueError("Legacy queued configuration hash mismatch")
            return config_yaml

    @staticmethod
    async def _insert_failed_queued_run(
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

    async def claim_run(
        self,
        run_id: str,
        job_id: str,
        trigger: str,
        config_hash: str,
        started_at: datetime,
    ) -> bool:
        """Atomically transition one queued delivery to an owned running run."""
        timestamp = fmt_dt(started_at)
        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
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
            config_yaml = reveal_text(
                cast(str, config_row["config_yaml"]),
                purpose=config_purpose,
            )
            if hashlib.sha256(config_yaml.encode("utf-8")).hexdigest() != config_hash:
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

    async def record_scheduled_trigger_failure(
        self,
        job_id: str,
        run_id: str,
        *,
        failed_at: datetime,
        failure_code: str,
    ) -> bool:
        """Persist one sanitized missed fire and the schedule's degraded state."""

        if _SCHEDULE_FAILURE_CODE_RE.fullmatch(failure_code) is None:
            raise ValueError("Scheduled failure code must be a bounded safe identifier")
        failed_text = fmt_dt(failed_at)
        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
            cursor = await db.execute(
                """SELECT config_hash, status, current_run_id
                   FROM jobs
                   WHERE job_id = ?
                     AND schedule_cron IS NOT NULL
                     AND schedule_enabled = 1
                     AND status NOT IN ('cancelled', 'deleting')""",
                (job_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                await db.rollback()
                return False
            cursor = await db.execute(
                """INSERT OR IGNORE INTO job_runs
                       (run_id, job_id, status, trigger, config_hash, config_yaml,
                        started_at, heartbeat_at, completed_at, record_count,
                        error_count, webhook_reconciled_at, failure_code)
                       VALUES (?, ?, 'failed', 'scheduled', ?, NULL, ?, ?, ?, 0, 1, ?, ?)""",
                (
                    run_id,
                    job_id,
                    cast(str, row["config_hash"]),
                    failed_text,
                    failed_text,
                    failed_text,
                    failed_text,
                    failure_code,
                ),
            )
            if cursor.rowcount == 0:
                existing = await (
                    await db.execute(
                        "SELECT status FROM job_runs WHERE job_id = ? AND run_id = ?",
                        (job_id, run_id),
                    )
                ).fetchone()
                if existing is None or existing["status"] != JobStatus.failed.value:
                    await db.rollback()
                    return False

            parent_can_converge = (
                row["status"]
                not in {
                    JobStatus.queued.value,
                    JobStatus.running.value,
                    JobStatus.cancelled.value,
                    JobStatus.deleting.value,
                }
                or (
                    row["status"] == JobStatus.queued.value
                    and row["current_run_id"] in {None, run_id}
                )
            )
            await db.execute(
                """UPDATE jobs
                   SET schedule_failure_at = ?,
                       schedule_failure_code = ?,
                       schedule_consecutive_failures = schedule_consecutive_failures + 1,
                       status = CASE WHEN ? THEN 'failed' ELSE status END,
                       current_run_id = CASE WHEN ? THEN ? ELSE current_run_id END,
                       current_trigger = CASE WHEN ? THEN 'scheduled' ELSE current_trigger END,
                       updated_at = CASE WHEN ? THEN ? ELSE updated_at END
                   WHERE job_id = ?""",
                (
                    failed_text,
                    failure_code,
                    int(parent_can_converge),
                    int(parent_can_converge),
                    run_id,
                    int(parent_can_converge),
                    int(parent_can_converge),
                    failed_text,
                    job_id,
                ),
            )
            return True

    async def clear_scheduled_trigger_failure(self, job_id: str) -> bool:
        """Clear degraded schedule state after a fire is accepted by Redis."""

        cursor = await self._execute_write(
            """UPDATE jobs
               SET schedule_failure_at = NULL,
                   schedule_failure_code = NULL,
                   schedule_consecutive_failures = 0
               WHERE job_id = ?
                 AND schedule_cron IS NOT NULL
                 AND schedule_failure_at IS NOT NULL""",
            (job_id,),
        )
        return cursor.rowcount == 1

    async def list_scheduled_trigger_failures(self) -> list[tuple[str, str]]:
        """Return unresolved failures for enabled, locally runnable schedules."""

        async with get_db("jobs.db") as db:
            cursor = await db.execute(
                """SELECT job_id, schedule_failure_code
                   FROM jobs
                   WHERE schedule_cron IS NOT NULL
                     AND schedule_enabled = 1
                     AND status NOT IN ('cancelled', 'deleting')
                     AND schedule_failure_at IS NOT NULL
                     AND schedule_failure_code IS NOT NULL
                   ORDER BY schedule_failure_at, job_id"""
            )
            return [
                (str(row["job_id"]), str(row["schedule_failure_code"]))
                for row in await cursor.fetchall()
            ]

    async def heartbeat_run(
        self,
        job_id: str,
        run_id: str,
        heartbeat_at: datetime,
    ) -> None:
        """Refresh the expected active run without touching the parent timestamp."""
        cursor = await self._execute_write(
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
            self._raise_ownership("heartbeat", job_id, run_id)

    async def finalize_owned_run(
        self,
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
        if status not in _TERMINAL_RUN_STATUSES:
            raise ValueError(f"Invalid terminal run status: {status!r}")
        if webhook_delivery is not None:
            self._validate_terminal_delivery(
                webhook_delivery,
                job_id=job_id,
                run_id=run_id,
                status=status,
            )
        completed_text = fmt_dt(completed_at)
        cutoff_text = fmt_dt(heartbeat_cutoff)
        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
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
                self._raise_ownership("finalize", job_id, run_id)
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
                self._raise_ownership("finalize", job_id, run_id)
            intent_created = False
            if webhook_delivery is not None:
                intent_created = await insert_webhook_delivery(
                    db,
                    webhook_delivery,
                    created_at=completed_at,
                )
                qualification_checkpoint("during_webhook_intent_transaction")
            await db.commit()
            if webhook_delivery is not None:
                logger.info(
                    "Terminal webhook intent persisted atomically "
                    "job_id=%s run_id=%s event=%s delivery_id=%s "
                    "terminal_status=%s recovery_action=%s",
                    job_id,
                    run_id,
                    webhook_delivery.event,
                    webhook_delivery.delivery_id,
                    status,
                    ("atomic_intent_created" if intent_created else "existing_intent_noop"),
                )

    async def fail_owned_run(
        self,
        job_id: str,
        run_id: str,
        failed_at: datetime,
        heartbeat_cutoff: datetime | None = None,
        *,
        error_count: int = 0,
        webhook_delivery: WebhookDeliveryCreate | None = None,
    ) -> None:
        """Fail matching ownership with optional atomic terminal intent."""
        if webhook_delivery is not None:
            self._validate_terminal_delivery(
                webhook_delivery,
                job_id=job_id,
                run_id=run_id,
                status=JobStatus.failed.value,
            )
        failed_text = fmt_dt(failed_at)
        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
            cursor = await db.execute(
                "SELECT status FROM jobs WHERE job_id = ? AND current_run_id = ?",
                (job_id, run_id),
            )
            row = await cursor.fetchone()
            if row is None:
                await db.rollback()
                self._raise_ownership("fail", job_id, run_id)
            job_status = str(row["status"])
            if job_status == JobStatus.queued.value:
                await self._insert_failed_queued_run(
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
                    self._raise_ownership("fail", job_id, run_id)
                intent_created = False
                if webhook_delivery is not None:
                    intent_created = await insert_webhook_delivery(
                        db,
                        webhook_delivery,
                        created_at=failed_at,
                    )
                await db.commit()
                if webhook_delivery is not None:
                    logger.info(
                        "Terminal webhook intent persisted atomically "
                        "job_id=%s run_id=%s event=%s delivery_id=%s "
                        "terminal_status=%s recovery_action=%s",
                        job_id,
                        run_id,
                        webhook_delivery.event,
                        webhook_delivery.delivery_id,
                        JobStatus.failed.value,
                        (
                            "atomic_intent_created"
                            if intent_created
                            else "existing_intent_noop"
                        ),
                    )
                return
            if job_status != JobStatus.running.value:
                await db.rollback()
                self._raise_ownership("fail", job_id, run_id)

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
                self._raise_ownership("fail", job_id, run_id)
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
                self._raise_ownership("fail", job_id, run_id)
            intent_created = False
            if webhook_delivery is not None:
                intent_created = await insert_webhook_delivery(
                    db,
                    webhook_delivery,
                    created_at=failed_at,
                )
            await db.commit()
            if webhook_delivery is not None:
                logger.info(
                    "Terminal webhook intent persisted atomically "
                    "job_id=%s run_id=%s event=%s delivery_id=%s "
                    "terminal_status=%s recovery_action=%s",
                    job_id,
                    run_id,
                    webhook_delivery.event,
                    webhook_delivery.delivery_id,
                    JobStatus.failed.value,
                    ("atomic_intent_created" if intent_created else "existing_intent_noop"),
                )

    async def list_terminal_webhook_candidates(
        self,
        limit: int | None = None,
    ) -> list[TerminalWebhookCandidate]:
        """Return terminal runs and their current logical outbox state."""

        async with get_db("jobs.db") as db:
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
            parameters: tuple[int, ...] = ()
            if limit is not None:
                if limit < 1:
                    raise ValueError("limit must be positive")
                query += " LIMIT ?"
                parameters = (limit,)
            cursor = await db.execute(query, parameters)
            rows = cast(list[Mapping[str, object]], await cursor.fetchall())

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
        self,
        candidate: TerminalWebhookCandidate,
        webhook_delivery: WebhookDeliveryCreate | None,
    ) -> TerminalIntentReconcileResult:
        """CAS-recheck terminal state, converge its parent, and ensure intent."""

        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
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
                or row["run_status"] not in _TERMINAL_RUN_STATUSES
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
                await self._mark_terminal_webhook_reconciled(db, candidate)
                await db.commit()
                return TerminalIntentReconcileResult(
                    TerminalIntentAction.existing,
                    parent_converged=parent_converged,
                    delivery_id=str(existing["delivery_id"]),
                )

            if webhook_delivery is None:
                await self._mark_terminal_webhook_reconciled(db, candidate)
                await db.commit()
                return TerminalIntentReconcileResult(
                    TerminalIntentAction.not_required,
                    parent_converged=parent_converged,
                )

            self._validate_terminal_delivery(
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
            await self._mark_terminal_webhook_reconciled(db, candidate)
            await db.commit()
            return TerminalIntentReconcileResult(
                (TerminalIntentAction.created if created else TerminalIntentAction.existing),
                parent_converged=parent_converged,
                delivery_id=webhook_delivery.delivery_id,
            )

    async def mark_terminal_webhook_reconciliation_failure(
        self,
        job_id: str,
        run_id: str,
        failed_at: datetime,
    ) -> bool:
        """Persist retry order so one bad candidate cannot monopolize a batch."""

        cursor = await self._execute_write(
            """UPDATE job_runs
               SET webhook_reconciliation_failed_at = ?
               WHERE job_id = ? AND run_id = ?
                 AND status IN ('complete', 'partial', 'failed')
                 AND webhook_reconciled_at IS NULL""",
            (fmt_dt(failed_at), job_id, run_id),
        )
        return cursor.rowcount == 1

    async def queue_run(
        self,
        job_id: str,
        *,
        expected_status: str,
        expected_run_id: str | None,
        new_run_id: str,
        new_trigger: str,
        queued_at: datetime,
        stale_before: datetime | None = None,
        expected_config_yaml: str | None = None,
    ) -> bool:
        """Queue a replacement only if the scheduler's observed state still holds."""
        if expected_status in {
            JobStatus.cancelled.value,
            JobStatus.deleting.value,
        }:
            return False
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
        if expected_config_yaml is not None:
            config_clause = " AND config_hash = ?"
            params.append(
                hashlib.sha256(expected_config_yaml.encode("utf-8")).hexdigest()
            )
        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
            cursor = await db.execute(
                """UPDATE jobs
                   SET status = 'queued', updated_at = ?, current_run_id = ?,
                       current_trigger = ?
                   WHERE job_id = ?
                     AND status = ?
                     AND current_run_id IS ?"""
                + stale_clause
                + config_clause,
                params,
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return False
            row = await (
                await db.execute(
                    "SELECT config_yaml, config_hash FROM jobs WHERE job_id = ?",
                    (job_id,),
                )
            ).fetchone()
            if row is None:  # pragma: no cover - transaction invariant
                raise RuntimeError("Queued parent disappeared inside its transaction")
            config_yaml = reveal_text(
                cast(str, row["config_yaml"]),
                purpose=f"jobs.config_yaml:{job_id}",
            )
            config_hash = cast(str, row["config_hash"])
            if hashlib.sha256(config_yaml.encode("utf-8")).hexdigest() != config_hash:
                raise ValueError("Queued parent configuration hash mismatch")
            await db.execute(
                """INSERT INTO queued_run_snapshots
                       (run_id, job_id, trigger, config_hash, config_yaml, queued_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    new_run_id,
                    job_id,
                    new_trigger,
                    config_hash,
                    protect_text(
                        config_yaml,
                        purpose=f"queued_run_snapshots.config_yaml:{new_run_id}",
                    ),
                    fmt_dt(queued_at),
                ),
            )
            return True

    async def fail_queued_run(
        self,
        job_id: str,
        run_id: str,
        failed_at: datetime,
    ) -> bool:
        """Fail an enqueue attempt only while its delivery is still current."""
        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
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
            await self._insert_failed_queued_run(
                db,
                job_id=job_id,
                run_id=run_id,
                failed_at=failed_at,
                error_count=1,
                failure_code="enqueue_failure",
            )
            return True

    async def list_stale_queued_jobs(
        self,
        stale_before: datetime,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[StaleQueuedJob]:
        """Return stale queued deliveries with all reconciliation inputs.

        ``jobs.updated_at`` is the persisted queued timestamp established by
        submission, scheduling, or an earlier recovery reservation. Rows with
        no owned run or no queued timestamp are deliberately excluded.
        """
        async with get_db("jobs.db") as db:
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
        self,
        job_id: str,
        run_id: str,
        *,
        expected_queued_at: datetime,
        stale_before: datetime,
        reserved_at: datetime,
    ) -> bool:
        """Reserve the exact stale snapshot while preserving its run ID."""
        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
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
        self,
        job_id: str,
        run_id: str,
        *,
        expected_queued_at: datetime,
        failed_at: datetime,
    ) -> bool:
        """Fail only the exact queued timestamp reconciliation still owns."""
        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
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
            await self._insert_failed_queued_run(
                db,
                job_id=job_id,
                run_id=run_id,
                failed_at=failed_at,
                error_count=1,
                failure_code="queued_reconciliation_failure",
            )
            return True

    @staticmethod
    def _heartbeat_is_stale(value: str | None, cutoff: datetime) -> bool:
        heartbeat = parse_dt(value)
        if heartbeat is None:
            return True
        comparable_cutoff = cutoff.replace(tzinfo=None) if heartbeat.tzinfo is None else cutoff
        return heartbeat <= comparable_cutoff

    async def _recover_current_run(
        self,
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

        if run_status in _TERMINAL_RUN_STATUSES:
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

        if run_status != JobStatus.running.value or not self._heartbeat_is_stale(
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
        self,
        job_id: str,
        run_id: str | None,
        heartbeat_cutoff: datetime,
        recovered_at: datetime,
    ) -> bool:
        """Recover one run only if it is still current and genuinely stale."""
        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
            recovery = await self._recover_current_run(
                db,
                job_id=job_id,
                expected_run_id=run_id,
                heartbeat_cutoff=heartbeat_cutoff,
                recovered_at=recovered_at,
            )
            return recovery is not None

    async def recover_stale_running_jobs(
        self,
        cutoff: datetime,
        recovered_at: datetime,
        limit: int | None = None,
    ) -> list[RunRecovery]:
        """Recover stale/invalid running state under one SQLite write lock."""
        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
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
                recovery = await self._recover_current_run(
                    db,
                    job_id=cast(str, row["job_id"]),
                    expected_run_id=cast(str | None, row["current_run_id"]),
                    heartbeat_cutoff=cutoff,
                    recovered_at=recovered_at,
                )
                if recovery is not None:
                    recoveries.append(recovery)
            return recoveries

    async def list_scheduled_jobs(self) -> list[tuple[str, str, str, bool]]:
        """Return ID, cron, timezone, and enabled state for scheduled jobs."""
        async with get_db("jobs.db") as db:
            cursor = await db.execute(SCHEDULED_JOBS_QUERY)
            rows = cast(list[Mapping[str, object]], await cursor.fetchall())
        return [row_to_schedule_state(row) for row in rows]

    async def rollback_queued_submission(self, job_id: str, run_id: str) -> bool:
        """Remove only a queued ad-hoc row whose Redis enqueue never succeeded."""

        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
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
