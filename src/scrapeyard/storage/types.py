"""Shared storage-layer data types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from scrapeyard.models.job import Job, JobStatus


class IdempotentJobAction(str, Enum):
    """Atomic outcome for caller-scoped ad-hoc submission creation."""

    created = "created"
    matched = "matched"
    conflict = "conflict"


@dataclass(frozen=True, slots=True)
class IdempotentJobOutcome:
    """Persisted idempotency decision and its authoritative job identity."""

    action: IdempotentJobAction
    job: Job
    run_id: str
    response_mode: str


class ScheduledJobMutationAction(str, Enum):
    """Atomic outcome for a scheduled-job configuration/state mutation."""

    updated = "updated"
    unchanged = "unchanged"
    missing = "missing"
    not_scheduled = "not_scheduled"
    active_conflict = "active_conflict"
    lifecycle_conflict = "lifecycle_conflict"


@dataclass(frozen=True, slots=True)
class ScheduledJobMutationOutcome:
    """Previous/current snapshots used for API consistency compensation."""

    action: ScheduledJobMutationAction
    previous: Job | None = None
    current: Job | None = None


class RunOwnershipError(RuntimeError):
    """Raised when a run-scoped mutation no longer owns the active job run."""

    def __init__(self, operation: str, job_id: str, run_id: str) -> None:
        self.operation = operation
        self.job_id = job_id
        self.run_id = run_id
        super().__init__(
            f"Run ownership lost during {operation}: job_id={job_id!r} run_id={run_id!r}"
        )


class CancellationAction(str, Enum):
    """Atomic jobs.db outcome for one cancellation request."""

    cancelled = "cancelled"
    already_cancelled = "already_cancelled"
    missing = "missing"
    conflict = "conflict"


@dataclass(frozen=True, slots=True)
class CancellationOutcome:
    """Authoritative cancellation snapshot returned by the job store."""

    action: CancellationAction
    job_id: str
    run_id: str | None
    prior_status: JobStatus | None
    resulting_status: JobStatus | None

    @property
    def queue_quiescence_required(self) -> bool:
        return self.action in {
            CancellationAction.cancelled,
            CancellationAction.already_cancelled,
        } and self.run_id is not None


class DeletionReservationAction(str, Enum):
    """Atomic jobs.db outcome for a resumable deletion reservation."""

    created = "created"
    resumed = "resumed"
    missing = "missing"
    active_conflict = "active_conflict"
    policy_conflict = "policy_conflict"
    pending_webhook_conflict = "pending_webhook_conflict"


@dataclass(frozen=True, slots=True)
class DeletionReservationOutcome:
    """Persisted deletion state and immutable cleanup policy."""

    action: DeletionReservationAction
    job_id: str
    run_id: str | None
    prior_status: JobStatus | None
    delete_results: bool


class DeletionFinalizationAction(str, Enum):
    """Outcome of the final all-jobs.db deletion transaction."""

    deleted = "deleted"
    missing = "missing"
    not_reserved = "not_reserved"
    policy_conflict = "policy_conflict"
    pending_webhook_conflict = "pending_webhook_conflict"


@dataclass(frozen=True, slots=True)
class DeletionFinalizationOutcome:
    """Typed final-deletion result used by the resumable API workflow."""

    action: DeletionFinalizationAction
    job_id: str


@dataclass(frozen=True, slots=True)
class RunRecovery:
    """One job/run state repaired by a conditional recovery pass."""

    job_id: str
    run_id: str | None
    action: str
    last_heartbeat_at: datetime | None


@dataclass(frozen=True, slots=True)
class StaleQueuedJob:
    """Persisted queued delivery context used by startup reconciliation."""

    job_id: str
    run_id: str
    config_yaml: str
    queued_at: datetime
    schedule_cron: str | None
    schedule_enabled: bool

    @property
    def trigger(self) -> str:
        """Classify the accepted delivery from state persisted in SQLite."""
        return "scheduled" if self.schedule_cron is not None else "adhoc"


@dataclass(frozen=True, slots=True)
class TerminalWebhookCandidate:
    """Persisted jobs.db snapshot used to reconcile one terminal run."""

    job_id: str
    run_id: str
    status: JobStatus
    project: str
    name: str
    config_yaml: str
    config_hash: str
    started_at: datetime
    heartbeat_at: datetime
    completed_at: datetime | None
    record_count: int | None
    error_count: int
    parent_status: JobStatus
    current_run_id: str | None
    existing_delivery_id: str | None
    existing_delivery_status: str | None
    existing_delivery_scrubbed_at: datetime | None


class TerminalIntentAction(str, Enum):
    """Outcome of one ownership/config-checked terminal reconciliation write."""

    created = "created"
    existing = "existing"
    not_required = "not_required"
    race_noop = "race_noop"


@dataclass(frozen=True, slots=True)
class TerminalIntentReconcileResult:
    """Atomic jobs.db reconciliation outcome for one terminal run."""

    action: TerminalIntentAction
    parent_converged: bool = False
    delivery_id: str | None = None


@dataclass(frozen=True, slots=True)
class ResultPayload:
    """Wrapper returned by get_result with run context."""

    run_id: str
    data: Any
    status: str = "complete"


@dataclass(frozen=True, slots=True)
class ResultMetadata:
    """Cross-database result metadata used for webhook payload enrichment."""

    job_id: str
    run_id: str
    status: str
    record_count: int | None
    file_path: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SaveResultMeta:
    """Metadata returned from a save_result call."""

    run_id: str
    file_path: str
    record_count: int | None
    serialized_bytes: int


class ResultArtifactFailureKind(str, Enum):
    """Stable storage-reconciliation classifications without sensitive details."""

    missing = "missing"
    corrupt = "corrupt"
    unreadable = "unreadable"
    unsafe = "unsafe"


@dataclass(frozen=True, slots=True)
class ResultArtifactFailure:
    """One metadata-backed artifact that could not be validated."""

    job_id: str
    run_id: str
    kind: ResultArtifactFailureKind
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationOperationFailure:
    """One filesystem scan/removal failure safe for structured logging."""

    action: str
    identifier: str
    error_type: str


@dataclass(frozen=True, slots=True)
class ResultReconciliationReport:
    """Deterministic result-artifact reconciliation outcome."""

    dry_run: bool
    metadata_rows_inspected: int = 0
    valid_artifacts: int = 0
    missing_result_files: int = 0
    corrupt_result_files: int = 0
    unreadable_result_files: int = 0
    unsafe_metadata_paths: int = 0
    filesystem_run_directories_inspected: int = 0
    malformed_entries_ignored: int = 0
    orphan_candidates: int = 0
    recent_candidates_skipped: int = 0
    active_run_candidates_skipped: int = 0
    metadata_race_candidates_skipped: int = 0
    active_run_race_candidates_skipped: int = 0
    stale_temporary_candidates: int = 0
    directories_would_remove: int = 0
    files_would_remove: int = 0
    directories_removed: int = 0
    files_removed: int = 0
    removed_bytes: int = 0
    artifact_failures: tuple[ResultArtifactFailure, ...] = ()
    operation_failures: tuple[ReconciliationOperationFailure, ...] = ()

    @property
    def failure_count(self) -> int:
        """Return metadata validation plus scan/removal failure count."""

        return len(self.artifact_failures) + len(self.operation_failures)
