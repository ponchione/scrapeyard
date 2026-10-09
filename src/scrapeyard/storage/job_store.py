"""SQLite-backed implementation of the JobStore protocol."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import NoReturn, cast

import aiosqlite

from scrapeyard.common.dt import fmt_dt
from scrapeyard.common.settings import get_settings
from scrapeyard.common.yaml import load_yaml_mapping
from scrapeyard.models.job import Job, JobRun, JobStatus, JobSummary
from scrapeyard.queue.memory import memory_headroom_mb
from scrapeyard.runtime.metrics import ADMISSION_REJECTIONS
from scrapeyard.storage import (
    job_deletion,
    job_recovery,
    job_retention,
    job_terminal_reconciliation,
)
from scrapeyard.storage.database import db_transaction, get_db
from scrapeyard.storage.job_queries import (
    PROJECT_SUMMARY_QUERY,
    SCHEDULED_JOBS_QUERY,
    build_list_jobs_with_stats_query,
)
from scrapeyard.storage.job_rows import (
    row_to_job,
    row_to_job_run,
    row_to_job_summary_with_stats,
    row_to_job_with_stats,
    row_to_project_summary,
    row_to_schedule_state,
)
from scrapeyard.storage.job_sql import JOB_COLUMNS, JOB_RUN_COLUMNS, select_columns
from scrapeyard.storage.types import (
    CancellationOutcome,
    CleanupBacklogSnapshot,
    DeletionFinalizationOutcome,
    DeletionReservationOutcome,
    HistoryPruneResult,
    IdempotentJobAction,
    IdempotentJobOutcome,
    RunOwnershipError,
    RunRecovery,
    ScheduledJobMutationAction,
    ScheduledJobMutationOutcome,
    StaleQueuedJob,
    TerminalIntentReconcileResult,
    TerminalWebhookCandidate,
)
from scrapeyard.storage.webhook_outbox import WebhookDeliveryCreate
from scrapeyard.storage.secret_envelope import protect_text, reveal_text


_SCHEDULE_FAILURE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
# Exact-run identities remembered by verified config hash (see get_job_run_state).
_RUN_IDENTITY_CACHE_ENTRIES = 256


def _reveal_verified(value: str, *, purpose: str, expected_hash: str) -> str | None:
    """Decrypt a stored config and return it only if it hashes to *expected_hash*."""

    config_yaml = reveal_text(value, purpose=purpose)
    if hashlib.sha256(config_yaml.encode("utf-8")).hexdigest() != expected_hash:
        return None
    return config_yaml


class DuplicateJobError(Exception):
    """Raised when a job name already exists within a project namespace."""

    def __init__(self, project: str, name: str) -> None:
        self.project = project
        self.name = name
        super().__init__(f"Job {name!r} already exists in project {project!r}")


class AdmissionCapacityError(RuntimeError):
    """A new run cannot be accepted; retry the same logical submission later."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        ADMISSION_REJECTIONS.labels(reason).inc()
        super().__init__(f"Scrape admission at capacity ({reason})")


def is_duplicate_job_integrity_error(error: str | Exception) -> bool:
    message = error if isinstance(error, str) else str(error)
    return (
        "UNIQUE constraint failed: jobs.project, jobs.name" in message
        or "jobs.project, jobs.name" in message
    )


class SQLiteJobStore:
    """SQLite implementation of :class:`~scrapeyard.storage.protocols.JobStore`."""

    def __init__(self) -> None:
        # config_hash -> (project, name) as parsed from that exact config.
        self._run_identities: OrderedDict[str, tuple[object, object]] = OrderedDict()

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
            await SQLiteJobStore._check_admission(db)
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
    async def _check_admission(db: aiosqlite.Connection) -> None:
        """Check the just-reserved run inside its write transaction.

        The current queued/running owner is the reservation. Terminal changes
        release it automatically; replay and delivery repair never reacquire it.
        """
        settings = get_settings()
        cursor = await db.execute(
            """SELECT COUNT(*) FROM jobs
               WHERE status IN ('queued', 'running') AND current_run_id IS NOT NULL"""
        )
        row = await cursor.fetchone()
        assert row is not None
        if row[0] > settings.workers_max_accepted_runs:
            raise AdmissionCapacityError("backlog")
        if memory_headroom_mb(settings.workers_memory_limit_mb) <= 0:
            raise AdmissionCapacityError("memory")

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

    async def save_job(self, job: Job) -> str:
        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
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

    async def get_submission_receipt(
        self, caller_scope: str, key_digest: str, observed_at: datetime,
    ) -> dict[str, object] | None:
        """Look up existing acceptance without creating/enqueueing or refreshing retention."""
        async with get_db("jobs.db") as db:
            cursor = await db.execute(
                """SELECT i.job_id, i.run_id, i.request_hash AS config_hash, j.project,
                          i.created_at, i.expires_at
                   FROM scrape_idempotency i JOIN jobs j ON j.job_id = i.job_id
                   WHERE i.caller_scope = ? AND i.key_digest = ? AND i.expires_at > ?""",
                (caller_scope, key_digest, fmt_dt(observed_at)),
            )
            row = await cursor.fetchone()
        return dict(row) if row is not None else None

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
            rows = await job_retention.cleanup_backlog_rows(
                db,
                observed_at=observed_at,
                adhoc_expired_before=adhoc_expired_before,
                scheduled_expired_before=scheduled_expired_before,
                tombstone_expired_before=tombstone_expired_before,
                max_scheduled_runs_per_job=max_scheduled_runs_per_job,
            )
        return job_retention.cleanup_backlog_snapshots(*rows)

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
            return await job_retention.list_adhoc_jobs_for_retention(
                db,
                expired_before,
                idempotency_observed_at=idempotency_observed_at,
                tombstone_expired_before=tombstone_expired_before,
                limit=limit,
            )

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
        async with get_db("jobs.db") as db:
            return await job_retention.list_scheduled_runs_for_retention(
                db,
                expired_before,
                tombstone_expired_before=tombstone_expired_before,
                max_runs_per_job=max_runs_per_job,
                limit=limit,
            )

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
        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
            return await job_retention.prune_scheduled_run_for_retention(
                db,
                job_id,
                run_id,
                expired_before=expired_before,
                tombstone_expired_before=tombstone_expired_before,
                max_runs_per_job=max_runs_per_job,
            )

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

    async def get_job_project(self, job_id: str) -> str:
        """Return a job's project without reading or decrypting its config."""
        async with get_db("jobs.db") as db:
            cursor = await db.execute("SELECT project FROM jobs WHERE job_id = ?", (job_id,))
            row = await cursor.fetchone()
        if row is None:
            raise KeyError(f"Job not found: {job_id!r}")
        return cast(str, row["project"])

    async def get_job_with_stats(self, job_id: str) -> tuple[Job, int, datetime | None]:
        """Return a job with its lifetime run count and last start in one read."""
        async with get_db("jobs.db") as db:
            cursor = await db.execute(
                f"SELECT {select_columns(JOB_COLUMNS)}, "
                "lifetime_run_count AS run_count, last_run_at "
                "FROM jobs WHERE job_id = ?",
                (job_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            raise KeyError(f"Job not found: {job_id!r}")
        return row_to_job_with_stats(cast(Mapping[str, object], row))

    async def cancel_job(
        self,
        job_id: str,
        cancelled_at: datetime,
        *,
        expected_run_id: str | None = None,
    ) -> CancellationOutcome:
        """Cancel the exact accepted delivery under one immediate transaction."""

        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
            return await job_deletion.cancel_job(
                db,
                job_id,
                cancelled_at,
                expected_run_id=expected_run_id,
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

        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
            return await job_deletion.reserve_job_deletion(
                db,
                job_id,
                delete_results=delete_results,
                requested_at=requested_at,
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
            return await job_deletion.finalize_job_deletion(
                db,
                job_id,
                delete_results=delete_results,
                preserve_idempotency_after=preserve_idempotency_after,
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

    async def get_job_run_state(self, job_id: str, run_id: str) -> dict[str, object] | None:
        """Read one accepted identity atomically, including unstarted cancellation."""
        async with get_db("jobs.db") as db:
            cursor = await db.execute(
                """SELECT j.job_id, j.project, j.name,
                          COALESCE(r.run_id, q.run_id) AS run_id,
                          COALESCE(r.config_hash, q.config_hash) AS config_hash,
                          COALESCE(r.status, j.status) AS status,
                          r.started_at, r.completed_at, r.failure_code,
                          COALESCE(r.config_yaml, q.config_yaml) AS config_yaml,
                          CASE WHEN r.run_id IS NOT NULL THEN 'job_runs' ELSE 'queued_run_snapshots' END AS origin
                   FROM jobs j
                   LEFT JOIN job_runs r ON r.job_id = j.job_id AND r.run_id = ?
                   LEFT JOIN queued_run_snapshots q
                     ON q.job_id = j.job_id AND q.run_id = ? AND q.run_id = j.current_run_id
                   WHERE j.job_id = ? AND (r.run_id IS NOT NULL OR q.run_id IS NOT NULL)""",
                (run_id, run_id, job_id),
            )
            row = await cursor.fetchone()
        if row is None or row["config_yaml"] is None:
            return None  # Historical missing snapshots remain unavailable evidence.
        state = dict(row)
        config_hash = cast(str, state["config_hash"])
        config_yaml = _reveal_verified(
            str(state.pop("config_yaml")),
            purpose=f"{state.pop('origin')}.config_yaml:{run_id}",
            expected_hash=config_hash,
        )
        if config_yaml is None:
            raise ValueError("Exact run configuration hash mismatch")
        # Callers poll this per in-flight run, and finding the name means
        # parsing the whole YAML (about 45 ms at 110 KB). A run's config never
        # changes, so parse each verified config once, off the event loop.
        identity = self._run_identities.get(config_hash)
        if identity is None:
            config = await asyncio.to_thread(load_yaml_mapping, config_yaml)
            identity = (config.get("project"), config.get("name"))
            self._run_identities[config_hash] = identity
            if len(self._run_identities) > _RUN_IDENTITY_CACHE_ENTRIES:
                self._run_identities.popitem(last=False)
        else:
            self._run_identities.move_to_end(config_hash)
        project, name = identity
        if project != state["project"] or not isinstance(name, str):
            raise ValueError("Exact run configuration identity mismatch")
        state["name"] = name
        return state

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
    ) -> list[tuple[JobSummary, int, datetime | None]]:
        """List jobs with lifetime run summaries, newest activity first."""
        sql, params = build_list_jobs_with_stats_query(project, limit, offset)
        async with get_db("jobs.db") as db:
            cursor = await db.execute(sql, params)
            rows = cast(list[Mapping[str, object]], await cursor.fetchall())
        return [row_to_job_summary_with_stats(row) for row in rows]

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
                config_yaml = _reveal_verified(
                    cast(str, row["config_yaml"]),
                    purpose=f"queued_run_snapshots.config_yaml:{run_id}",
                    expected_hash=cast(str, row["config_hash"]),
                )
                if config_yaml is None:
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
            config_yaml = _reveal_verified(
                cast(str, legacy["config_yaml"]),
                purpose=f"jobs.config_yaml:{job_id}",
                expected_hash=cast(str, legacy["config_hash"]),
            )
            if config_yaml is None:
                raise ValueError("Legacy queued configuration hash mismatch")
            return config_yaml

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
            config_yaml = _reveal_verified(
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
        if status not in job_terminal_reconciliation.TERMINAL_RUN_STATUSES:
            raise ValueError(f"Invalid terminal run status: {status!r}")
        if webhook_delivery is not None:
            job_terminal_reconciliation.validate_terminal_delivery(
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
            job_terminal_reconciliation.validate_terminal_delivery(
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
                    self._raise_ownership("fail", job_id, run_id)
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
            await job_terminal_reconciliation.commit_terminal_intent(
                db,
                webhook_delivery,
                job_id=job_id,
                run_id=run_id,
                status=JobStatus.failed.value,
                created_at=failed_at,
            )

    async def list_terminal_webhook_candidates(
        self,
        limit: int | None = None,
    ) -> list[TerminalWebhookCandidate]:
        """Return terminal runs and their current logical outbox state."""

        async with get_db("jobs.db") as db:
            rows = await job_terminal_reconciliation.terminal_webhook_candidate_rows(db, limit)
        return job_terminal_reconciliation.terminal_webhook_candidates(rows)

    async def reconcile_terminal_webhook_candidate(
        self,
        candidate: TerminalWebhookCandidate,
        webhook_delivery: WebhookDeliveryCreate | None,
    ) -> TerminalIntentReconcileResult:
        """CAS-recheck terminal state, converge its parent, and ensure intent."""

        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
            return await job_terminal_reconciliation.reconcile_terminal_webhook_candidate(
                db,
                candidate,
                webhook_delivery,
            )

    async def mark_terminal_webhook_reconciliation_failure(
        self,
        job_id: str,
        run_id: str,
        failed_at: datetime,
    ) -> bool:
        """Persist retry order so one bad candidate cannot monopolize a batch."""

        async with get_db("jobs.db") as db, db_transaction(db):
            return await job_terminal_reconciliation.mark_terminal_webhook_reconciliation_failure(
                db,
                job_id,
                run_id,
                failed_at,
            )

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
            await self._check_admission(db)
            row = await (
                await db.execute(
                    "SELECT config_yaml, config_hash FROM jobs WHERE job_id = ?",
                    (job_id,),
                )
            ).fetchone()
            if row is None:  # pragma: no cover - transaction invariant
                raise RuntimeError("Queued parent disappeared inside its transaction")
            config_hash = cast(str, row["config_hash"])
            config_yaml = _reveal_verified(
                cast(str, row["config_yaml"]),
                purpose=f"jobs.config_yaml:{job_id}",
                expected_hash=config_hash,
            )
            if config_yaml is None:
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
            await job_recovery.insert_failed_queued_run(
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
            rows = await job_recovery.stale_queued_rows(
                db,
                stale_before,
                limit=limit,
                offset=offset,
            )
        return job_recovery.stale_queued_jobs(rows)

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
            return await job_recovery.reserve_queued_run_recovery(
                db,
                job_id,
                run_id,
                expected_queued_at=expected_queued_at,
                stale_before=stale_before,
                reserved_at=reserved_at,
            )

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
            return await job_recovery.fail_queued_run_recovery(
                db,
                job_id,
                run_id,
                expected_queued_at=expected_queued_at,
                failed_at=failed_at,
            )

    async def recover_stale_run(
        self,
        job_id: str,
        run_id: str | None,
        heartbeat_cutoff: datetime,
        recovered_at: datetime,
    ) -> bool:
        """Recover one run only if it is still current and genuinely stale."""
        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
            return await job_recovery.recover_stale_run(
                db,
                job_id,
                run_id,
                heartbeat_cutoff,
                recovered_at,
            )

    async def recover_stale_running_jobs(
        self,
        cutoff: datetime,
        recovered_at: datetime,
        limit: int | None = None,
    ) -> list[RunRecovery]:
        """Recover stale/invalid running state under one SQLite write lock."""
        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
            return await job_recovery.recover_stale_running_jobs(
                db,
                cutoff,
                recovered_at,
                limit,
            )

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
