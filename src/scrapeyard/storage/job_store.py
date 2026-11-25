"""SQLite-backed implementation of the JobStore protocol."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime
from typing import cast

import aiosqlite

from scrapeyard.common.yaml import load_yaml_mapping
from scrapeyard.models.job import Job, JobMetadata, JobRun, JobStatus, JobSummary
from scrapeyard.runtime.metrics import ADMISSION_REJECTIONS
from scrapeyard.storage import (
    job_crud,
    job_deletion,
    job_recovery,
    job_retention,
    job_runs,
    job_schedules,
    job_terminal_reconciliation,
)
from scrapeyard.storage.database import db_transaction, get_db
from scrapeyard.storage.job_queries import PROJECT_SUMMARY_QUERY, build_list_jobs_with_stats_query
from scrapeyard.storage.job_rows import (
    row_to_job_summary_with_stats,
    row_to_job_with_stats,
    row_to_project_summary,
)
from scrapeyard.storage.job_sql import JOB_COLUMNS, select_columns
from scrapeyard.storage.secret_envelope import reveal_text
from scrapeyard.storage.types import (
    CancellationOutcome,
    CleanupBacklogSnapshot,
    DeletionFinalizationOutcome,
    DeletionReservationOutcome,
    HistoryPruneResult,
    IdempotentJobOutcome,
    RunRecovery,
    ScheduledJobMutationOutcome,
    StaleQueuedJob,
    TerminalIntentReconcileResult,
    TerminalWebhookCandidate,
)
from scrapeyard.storage.webhook_outbox import WebhookDeliveryCreate


def _reveal_verified(value: str, *, purpose: str, expected_hash: str) -> str | None:
    """Decrypt a stored config and return it only if it hashes to *expected_hash*."""
    config_yaml = reveal_text(value, purpose=purpose)
    if hashlib.sha256(config_yaml.encode("utf-8")).hexdigest() != expected_hash:
        return None
    return config_yaml


def _reveal_verified_identity(
    value: str,
    *,
    purpose: str,
    expected_hash: str,
    cached_identity: tuple[object, object] | None,
) -> tuple[object, object] | None:
    config_yaml = _reveal_verified(value, purpose=purpose, expected_hash=expected_hash)
    if config_yaml is None:
        return None
    if cached_identity is not None:
        return cached_identity
    config = load_yaml_mapping(config_yaml)
    return (config.get("project"), config.get("name"))


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
        self._run_state_cache = job_runs.RunStateCache()

    @staticmethod
    async def _prepare_job_insert_crypto(job: Job) -> tuple[str, str, str | None]:
        snapshot_purpose = (
            f"queued_run_snapshots.config_yaml:{job.current_run_id}"
            if job.status is JobStatus.queued and job.current_run_id is not None
            else None
        )
        return await job_crud.prepare_config_crypto_async(
            job.config_yaml,
            job_purpose=f"jobs.config_yaml:{job.job_id}",
            snapshot_purpose=snapshot_purpose,
        )

    @staticmethod
    async def _check_admission(db: aiosqlite.Connection) -> None:
        await job_crud.check_admission(db, capacity_error_cls=AdmissionCapacityError)

    # --- Core job CRUD & idempotency ---

    async def save_job(self, job: Job) -> str:
        prepared = await self._prepare_job_insert_crypto(job)
        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
            try:
                await job_crud.insert_job(
                    db, job, prepared=prepared, capacity_error_cls=AdmissionCapacityError
                )
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
        if job.current_run_id is None:
            raise ValueError("Idempotent ad-hoc jobs require current_run_id")
        prepared = await self._prepare_job_insert_crypto(job)
        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
            return await job_crud.create_idempotent_job(
                db,
                job,
                prepared=prepared,
                caller_scope=caller_scope,
                key_digest=key_digest,
                request_hash=request_hash,
                response_mode=response_mode,
                expires_at=expires_at,
                is_duplicate_error=is_duplicate_job_integrity_error,
                duplicate_error_cls=DuplicateJobError,
                capacity_error_cls=AdmissionCapacityError,
            )

    async def get_submission_receipt(
        self, caller_scope: str, key_digest: str, observed_at: datetime
    ) -> dict[str, object] | None:
        async with get_db("jobs.db", readonly=True) as db:
            return await job_crud.get_submission_receipt(db, caller_scope, key_digest, observed_at)

    async def delete_expired_idempotency_records(
        self, expired_before: datetime, *, limit: int
    ) -> int:
        if limit < 1:
            raise ValueError("Idempotency cleanup limit must be positive")
        async with get_db("jobs.db") as db, db_transaction(db):
            return await job_crud.delete_expired_idempotency_records(
                db, expired_before, limit=limit
            )

    async def get_job(self, job_id: str) -> Job:
        async with get_db("jobs.db", readonly=True) as db:
            job = await job_crud.get_job_in_db(db, job_id)
        if job is None:
            raise KeyError(f"Job not found: {job_id!r}")
        return job

    async def get_job_metadata(self, job_id: str) -> JobMetadata:
        async with get_db("jobs.db", readonly=True) as db:
            metadata = await job_crud.get_job_metadata_in_db(db, job_id)
        if metadata is None:
            raise KeyError(f"Job not found: {job_id!r}")
        return metadata

    async def get_job_project(self, job_id: str) -> str:
        async with get_db("jobs.db", readonly=True) as db:
            cursor = await db.execute("SELECT project FROM jobs WHERE job_id = ?", (job_id,))
            row = await cursor.fetchone()
        if row is None:
            raise KeyError(f"Job not found: {job_id!r}")
        return cast(str, row["project"])

    async def get_job_with_stats(self, job_id: str) -> tuple[Job, int, datetime | None]:
        async with get_db("jobs.db", readonly=True) as db:
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

    async def list_jobs_with_stats(
        self, project: str | None = None, limit: int | None = None, offset: int = 0
    ) -> list[tuple[JobSummary, int, datetime | None]]:
        sql, params = build_list_jobs_with_stats_query(project, limit, offset)
        async with get_db("jobs.db", readonly=True) as db:
            cursor = await db.execute(sql, params)
            rows = cast(list[Mapping[str, object]], await cursor.fetchall())
        return [row_to_job_summary_with_stats(row) for row in rows]

    async def summary_by_project(self) -> list[tuple[str, str, int]]:
        async with get_db("jobs.db", readonly=True) as db:
            cursor = await db.execute(PROJECT_SUMMARY_QUERY)
            rows = cast(list[Mapping[str, object]], await cursor.fetchall())
        return [row_to_project_summary(row) for row in rows]

    async def get_job_run_state(self, job_id: str, run_id: str) -> dict[str, object] | None:
        async with get_db("jobs.db", readonly=True) as db:
            row = await job_runs.fetch_job_run_state_row(db, job_id, run_id)
        return await self._run_state_cache.resolve_state(
            row, job_id=job_id, run_id=run_id, reveal_identity=_reveal_verified_identity
        )

    # --- Retention delegates ---

    async def summarize_cleanup_backlog(
        self,
        *,
        observed_at: datetime,
        adhoc_expired_before: datetime,
        scheduled_expired_before: datetime,
        tombstone_expired_before: datetime,
        max_scheduled_runs_per_job: int,
    ) -> dict[str, CleanupBacklogSnapshot]:
        async with get_db("jobs.db", readonly=True) as db:
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
        if limit < 1:
            raise ValueError("Ad-hoc job cleanup limit must be positive")
        async with get_db("jobs.db", readonly=True) as db:
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
        if max_runs_per_job < 1 or limit < 1:
            raise ValueError("Scheduled run retention bounds must be positive")
        async with get_db("jobs.db", readonly=True) as db:
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

    # --- Scheduled job delegates ---

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
        config_hash, protected_config, _ = await job_crud.prepare_config_crypto_async(
            config_yaml, job_purpose=f"jobs.config_yaml:{job_id}"
        )
        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
            return await job_schedules.update_scheduled_job(
                db,
                job_id,
                project=project,
                name=name,
                config_yaml=config_yaml,
                config_hash=config_hash,
                protected_config=protected_config,
                schedule_cron=schedule_cron,
                schedule_timezone=schedule_timezone,
                schedule_enabled=schedule_enabled,
                updated_at=updated_at,
                get_job_in_db=job_crud.get_job_in_db,
                is_duplicate_error=is_duplicate_job_integrity_error,
                duplicate_error_cls=DuplicateJobError,
            )

    async def set_schedule_enabled(
        self, job_id: str, *, enabled: bool, updated_at: datetime
    ) -> ScheduledJobMutationOutcome:
        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
            return await job_schedules.set_schedule_enabled(
                db,
                job_id,
                enabled=enabled,
                updated_at=updated_at,
                get_metadata_in_db=job_crud.get_job_metadata_in_db,
            )

    async def restore_scheduled_job(
        self, expected: Job | JobMetadata, previous: Job | JobMetadata
    ) -> bool:
        expected_hash = (
            expected.config_hash
            if isinstance(expected, JobMetadata)
            else hashlib.sha256(expected.config_yaml.encode("utf-8")).hexdigest()
        )
        previous_hash: str | None = None
        previous_ciphertext: str | None = None
        if not isinstance(previous, JobMetadata):
            previous_hash, previous_ciphertext, _ = await job_crud.prepare_config_crypto_async(
                previous.config_yaml, job_purpose=f"jobs.config_yaml:{previous.job_id}"
            )
        async with get_db("jobs.db") as db, db_transaction(db):
            return await job_schedules.restore_scheduled_job(
                db,
                expected,
                previous,
                expected_hash=expected_hash,
                previous_hash=previous_hash,
                previous_ciphertext=previous_ciphertext,
            )

    async def rollback_scheduled_job_creation(self, job_id: str) -> bool:
        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
            return await job_schedules.rollback_scheduled_job_creation(db, job_id)

    async def record_scheduled_trigger_failure(
        self, job_id: str, run_id: str, *, failed_at: datetime, failure_code: str
    ) -> bool:
        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
            return await job_schedules.record_scheduled_trigger_failure(
                db, job_id, run_id, failed_at=failed_at, failure_code=failure_code
            )

    async def clear_scheduled_trigger_failure(self, job_id: str) -> bool:
        async with get_db("jobs.db") as db, db_transaction(db):
            return await job_schedules.clear_scheduled_trigger_failure(db, job_id)

    async def list_scheduled_trigger_failures(self) -> list[tuple[str, str]]:
        async with get_db("jobs.db", readonly=True) as db:
            return await job_schedules.list_scheduled_trigger_failures(db)

    async def list_scheduled_jobs(self) -> list[tuple[str, str, str, bool]]:
        async with get_db("jobs.db", readonly=True) as db:
            return await job_schedules.list_scheduled_jobs(db)

    # --- Cancellation & deletion delegates ---

    async def cancel_job(
        self, job_id: str, cancelled_at: datetime, *, expected_run_id: str | None = None
    ) -> CancellationOutcome:
        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
            return await job_deletion.cancel_job(
                db, job_id, cancelled_at, expected_run_id=expected_run_id
            )

    async def reserve_job_deletion(
        self, job_id: str, *, delete_results: bool, requested_at: datetime
    ) -> DeletionReservationOutcome:
        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
            return await job_deletion.reserve_job_deletion(
                db, job_id, delete_results=delete_results, requested_at=requested_at
            )

    async def finalize_job_deletion(
        self,
        job_id: str,
        *,
        delete_results: bool,
        preserve_idempotency_after: datetime | None = None,
    ) -> DeletionFinalizationOutcome:
        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
            return await job_deletion.finalize_job_deletion(
                db,
                job_id,
                delete_results=delete_results,
                preserve_idempotency_after=preserve_idempotency_after,
            )

    # --- Run lifecycle delegates ---

    async def run_is_active(self, job_id: str, run_id: str) -> bool:
        async with get_db("jobs.db", readonly=True) as db:
            return await job_runs.run_is_active(db, job_id, run_id)

    async def result_run_is_active(self, project: str, job_name: str, run_id: str) -> bool:
        async with get_db("jobs.db", readonly=True) as db:
            return await job_runs.result_run_is_active(db, project, job_name, run_id)

    async def get_job_runs(self, job_id: str, limit: int = 10) -> list[JobRun]:
        async with get_db("jobs.db", readonly=True) as db:
            return await job_runs.get_job_runs(db, job_id, limit)

    async def get_job_run(self, job_id: str, run_id: str) -> JobRun | None:
        async with get_db("jobs.db", readonly=True) as db:
            return await job_runs.get_job_run(db, job_id, run_id)

    async def get_queued_run_config(self, job_id: str, run_id: str) -> str:
        async with get_db("jobs.db", readonly=True) as db:
            return await job_runs.get_queued_run_config(
                db, job_id, run_id, reveal_verified=_reveal_verified
            )

    async def claim_run(
        self, run_id: str, job_id: str, trigger: str, config_hash: str, started_at: datetime
    ) -> bool:
        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
            return await job_runs.claim_run(
                db,
                run_id,
                job_id,
                trigger,
                config_hash,
                started_at,
                reveal_verified=_reveal_verified,
            )

    async def heartbeat_run(self, job_id: str, run_id: str, heartbeat_at: datetime) -> None:
        async with get_db("jobs.db") as db, db_transaction(db):
            await job_runs.heartbeat_run(db, job_id, run_id, heartbeat_at)

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
        if status not in job_terminal_reconciliation.TERMINAL_RUN_STATUSES:
            raise ValueError(f"Invalid terminal run status: {status!r}")
        if webhook_delivery is not None:
            job_terminal_reconciliation.validate_terminal_delivery(
                webhook_delivery, job_id=job_id, run_id=run_id, status=status
            )
        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
            await job_runs.finalize_owned_run(
                db,
                job_id,
                run_id,
                status,
                record_count,
                error_count,
                completed_at,
                heartbeat_cutoff,
                webhook_delivery=webhook_delivery,
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
        if webhook_delivery is not None:
            job_terminal_reconciliation.validate_terminal_delivery(
                webhook_delivery,
                job_id=job_id,
                run_id=run_id,
                status=JobStatus.failed.value,
            )
        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
            await job_runs.fail_owned_run(
                db,
                job_id,
                run_id,
                failed_at,
                heartbeat_cutoff,
                error_count=error_count,
                webhook_delivery=webhook_delivery,
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
        require_schedule_enabled: bool = False,
    ) -> bool:
        if expected_status in {JobStatus.cancelled.value, JobStatus.deleting.value}:
            return False
        expected_hash: str | None = None
        prepared_snapshot: str | None = None
        if expected_config_yaml is not None:
            expected_hash, prepared_snapshot, _ = await job_crud.prepare_config_crypto_async(
                expected_config_yaml, job_purpose=f"queued_run_snapshots.config_yaml:{new_run_id}"
            )
        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
            return await job_runs.queue_run(
                db,
                job_id,
                expected_status=expected_status,
                expected_run_id=expected_run_id,
                new_run_id=new_run_id,
                new_trigger=new_trigger,
                queued_at=queued_at,
                stale_before=stale_before,
                expected_hash=expected_hash,
                prepared_snapshot=prepared_snapshot,
                require_schedule_enabled=require_schedule_enabled,
                check_admission=self._check_admission,
                reveal_verified=_reveal_verified,
            )

    async def fail_queued_run(self, job_id: str, run_id: str, failed_at: datetime) -> bool:
        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
            return await job_runs.fail_queued_run(db, job_id, run_id, failed_at)

    async def rollback_queued_submission(self, job_id: str, run_id: str) -> bool:
        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
            return await job_runs.rollback_queued_submission(db, job_id, run_id)

    # --- Terminal webhook & recovery delegates ---

    async def list_terminal_webhook_candidates(
        self, limit: int | None = None
    ) -> list[TerminalWebhookCandidate]:
        async with get_db("jobs.db", readonly=True) as db:
            rows = await job_terminal_reconciliation.terminal_webhook_candidate_rows(db, limit)
        return job_terminal_reconciliation.terminal_webhook_candidates(rows)

    async def reconcile_terminal_webhook_candidate(
        self,
        candidate: TerminalWebhookCandidate,
        webhook_delivery: WebhookDeliveryCreate | None,
    ) -> TerminalIntentReconcileResult:
        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
            return await job_terminal_reconciliation.reconcile_terminal_webhook_candidate(
                db, candidate, webhook_delivery
            )

    async def mark_terminal_webhook_reconciliation_failure(
        self, job_id: str, run_id: str, failed_at: datetime
    ) -> bool:
        async with get_db("jobs.db") as db, db_transaction(db):
            return await job_terminal_reconciliation.mark_terminal_webhook_reconciliation_failure(
                db, job_id, run_id, failed_at
            )

    async def list_stale_queued_jobs(
        self, stale_before: datetime, *, limit: int | None = None, offset: int = 0
    ) -> list[StaleQueuedJob]:
        async with get_db("jobs.db", readonly=True) as db:
            rows = await job_recovery.stale_queued_rows(
                db, stale_before, limit=limit, offset=offset
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
        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
            return await job_recovery.fail_queued_run_recovery(
                db, job_id, run_id, expected_queued_at=expected_queued_at, failed_at=failed_at
            )

    async def recover_stale_run(
        self,
        job_id: str,
        run_id: str | None,
        heartbeat_cutoff: datetime,
        recovered_at: datetime,
    ) -> bool:
        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
            return await job_recovery.recover_stale_run(
                db, job_id, run_id, heartbeat_cutoff, recovered_at
            )

    async def recover_stale_running_jobs(
        self, cutoff: datetime, recovered_at: datetime, limit: int | None = None
    ) -> list[RunRecovery]:
        async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
            return await job_recovery.recover_stale_running_jobs(db, cutoff, recovered_at, limit)
