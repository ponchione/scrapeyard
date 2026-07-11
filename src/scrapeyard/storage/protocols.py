"""Storage protocol definitions for cloud-ready abstraction (spec section 9.1)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from scrapeyard.common.budgets import RunBudget
from scrapeyard.models.job import ErrorFilters, ErrorRecord, Job, JobRun
from scrapeyard.storage.types import (
    CancellationOutcome,
    DeletionFinalizationOutcome,
    DeletionReservationOutcome,
    ResultMetadata,
    ResultPayload,
    ResultReconciliationReport,
    RunRecovery,
    SaveResultMeta,
    StaleQueuedJob,
    TerminalIntentReconcileResult,
    TerminalWebhookCandidate,
)
from scrapeyard.storage.webhook_outbox import (
    WebhookDelivery,
    WebhookDeliveryCreate,
    WebhookFailureReason,
    WebhookOutboxSummary,
    WebhookRetentionSummary,
)


class JobStore(Protocol):
    """Async interface for job persistence."""

    async def save_job(self, job: Job) -> str: ...

    async def get_job(self, job_id: str) -> Job: ...

    async def rollback_queued_submission(self, job_id: str, run_id: str) -> bool:
        """Remove only a never-accepted queued submission after enqueue failure."""
        ...

    async def cancel_job(self, job_id: str, cancelled_at: datetime) -> CancellationOutcome:
        """Atomically cancel the exact current queued or running delivery."""
        ...

    async def run_is_active(self, job_id: str, run_id: str) -> bool:
        """Return whether the exact job/run pair still owns running state."""
        ...

    async def result_run_is_active(
        self,
        project: str,
        job_name: str,
        run_id: str,
    ) -> bool:
        """Return whether a queued/running parent owns this artifact identity."""
        ...

    async def reserve_job_deletion(
        self,
        job_id: str,
        *,
        delete_results: bool,
        requested_at: datetime,
    ) -> DeletionReservationOutcome:
        """Reserve or resume cross-database deletion under one immutable policy."""
        ...

    async def finalize_job_deletion(
        self,
        job_id: str,
        *,
        delete_results: bool,
    ) -> DeletionFinalizationOutcome:
        """Atomically remove terminal webhook rows, runs, intent, and parent."""
        ...

    async def get_job_runs(
        self, job_id: str, limit: int = 10,
    ) -> list[JobRun]: ...

    async def get_job_run_stats(
        self, job_id: str,
    ) -> tuple[int, datetime | None]: ...

    async def list_jobs_with_stats(
        self,
        project: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[tuple[Job, int, datetime | None]]: ...

    async def summary_by_project(self) -> list[tuple[str, str, int]]: ...

    async def claim_run(
        self,
        run_id: str,
        job_id: str,
        trigger: str,
        config_hash: str,
        started_at: datetime,
    ) -> bool:
        """Atomically claim a queued delivery and create its active run."""
        ...

    async def heartbeat_run(
        self,
        job_id: str,
        run_id: str,
        heartbeat_at: datetime,
    ) -> None:
        """Refresh an active run or raise RunOwnershipError."""
        ...

    async def get_job_run(self, job_id: str, run_id: str) -> JobRun | None: ...

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
        """Atomically finalize the expected run, parent, and optional intent."""
        ...

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
        """Conditionally fail owned state with optional atomic terminal intent."""
        ...

    async def list_terminal_webhook_candidates(
        self,
    ) -> list[TerminalWebhookCandidate]:
        """Return terminal runs and current logical-intent state deterministically."""
        ...

    async def reconcile_terminal_webhook_candidate(
        self,
        candidate: TerminalWebhookCandidate,
        webhook_delivery: WebhookDeliveryCreate | None,
    ) -> TerminalIntentReconcileResult:
        """Recheck terminal/config state, converge parent, and ensure intent."""
        ...

    async def queue_run(
        self,
        job_id: str,
        *,
        expected_status: str,
        expected_run_id: str | None,
        new_run_id: str,
        queued_at: datetime,
        stale_before: datetime | None = None,
    ) -> bool:
        """Conditionally replace the current job delivery with a queued run."""
        ...

    async def fail_queued_run(
        self,
        job_id: str,
        run_id: str,
        failed_at: datetime,
    ) -> bool:
        """Fail only the expected still-queued delivery."""
        ...

    async def list_stale_queued_jobs(
        self,
        stale_before: datetime,
    ) -> list[StaleQueuedJob]:
        """Return complete persisted context for stale owned queued deliveries."""
        ...

    async def reserve_queued_run_recovery(
        self,
        job_id: str,
        run_id: str,
        *,
        expected_queued_at: datetime,
        stale_before: datetime,
        reserved_at: datetime,
    ) -> bool:
        """Refresh only the exact stale queued snapshot selected for recovery."""
        ...

    async def fail_queued_run_recovery(
        self,
        job_id: str,
        run_id: str,
        *,
        expected_queued_at: datetime,
        failed_at: datetime,
    ) -> bool:
        """Fail only the exact queued snapshot owned by reconciliation."""
        ...

    async def recover_stale_run(
        self,
        job_id: str,
        run_id: str | None,
        heartbeat_cutoff: datetime,
        recovered_at: datetime,
    ) -> bool:
        """Recover only the matching run if its heartbeat remains stale."""
        ...

    async def recover_stale_running_jobs(
        self,
        cutoff: datetime,
        recovered_at: datetime,
    ) -> list[RunRecovery]:
        """Conditionally repair stale running jobs and describe each mutation."""
        ...

    async def list_scheduled_jobs(
        self,
    ) -> list[tuple[str, str, bool]]:
        """Return (job_id, schedule_cron, schedule_enabled) for all scheduled jobs."""
        ...


class ResultStore(Protocol):
    """Async interface for scrape result persistence."""

    async def save_result(
        self,
        job_id: str,
        data: Any,
        *,
        run_id: str | None = None,
        status: str = "complete",
        record_count: int | None = None,
        budget: RunBudget | None = None,
        max_serialized_bytes: int | None = None,
    ) -> SaveResultMeta: ...

    async def get_result(
        self, job_id: str, run_id: str | None = None,
    ) -> ResultPayload: ...

    async def get_result_metadata(
        self,
        job_id: str,
        run_id: str,
    ) -> ResultMetadata | None:
        """Return metadata without requiring the result artifact to be readable."""
        ...

    async def delete_results(self, job_id: str) -> None: ...

    async def delete_result(self, job_id: str, run_id: str) -> bool:
        """Delete one run-specific artifact and metadata row if present."""
        ...

    async def delete_expired(self, retention_days: int) -> int: ...

    async def prune_excess_per_job(self, max_results_per_job: int) -> int: ...

    async def reconcile_artifacts(
        self,
        *,
        grace_seconds: int,
        dry_run: bool,
        now: datetime | None = None,
    ) -> ResultReconciliationReport:
        """Validate metadata and reconcile stale contained filesystem artifacts."""
        ...


class ErrorStore(Protocol):
    """Async interface for structured error record persistence."""

    async def log_error(self, error: ErrorRecord) -> None: ...

    async def log_errors(self, errors: list[ErrorRecord]) -> None: ...

    async def query_errors(
        self,
        filters: ErrorFilters,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[ErrorRecord]: ...

    async def count_errors_for_run(self, run_id: str) -> int: ...

    async def delete_errors_for_job(self, job_id: str) -> None: ...


class WebhookOutboxStore(Protocol):
    """Async interface for durable webhook delivery persistence."""

    async def enqueue_delivery(
        self,
        delivery: WebhookDeliveryCreate,
        *,
        now: datetime | None = None,
    ) -> None: ...

    async def get_delivery(self, delivery_id: str) -> WebhookDelivery | None: ...

    async def list_pending(self, *, limit: int | None = None) -> list[WebhookDelivery]: ...

    async def list_due_pending(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> list[WebhookDelivery]: ...

    async def list_exhausted_pending(
        self,
        *,
        attempts_gte: int,
        created_at_lte: datetime,
        limit: int,
    ) -> list[WebhookDelivery]: ...

    async def next_pending_due_at(self) -> datetime | None: ...

    async def oldest_pending_created_at(self) -> datetime | None: ...

    async def summarize(
        self,
        *,
        now: datetime | None = None,
    ) -> WebhookOutboxSummary: ...

    async def begin_attempt(
        self,
        delivery_id: str,
        *,
        expected_attempts: int,
        attempted_at: datetime,
    ) -> WebhookDelivery | None: ...

    async def mark_delivered(
        self,
        delivery_id: str,
        *,
        delivered_at: datetime,
        expected_attempts: int | None = None,
        attempts: int = 1,
    ) -> bool: ...

    async def mark_retryable_failure(
        self,
        delivery_id: str,
        *,
        attempted_at: datetime,
        next_attempt_at: datetime,
        last_error: str,
        expected_attempts: int | None = None,
        attempts: int = 1,
    ) -> bool: ...

    async def mark_failed(
        self,
        delivery_id: str,
        *,
        failed_at: datetime,
        reason: WebhookFailureReason,
        last_error: str | None,
        expected_attempts: int | None = None,
        attempts: int = 0,
    ) -> bool: ...

    async def scrub_terminal_deliveries(
        self,
        *,
        delivered_before: datetime,
        failed_before: datetime,
        scrubbed_at: datetime,
        limit: int,
    ) -> WebhookRetentionSummary: ...
