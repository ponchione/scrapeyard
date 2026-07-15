"""Periodic cleanup of expired scrape results."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from scrapeyard.common.settings import get_settings
from scrapeyard.common.time import utc_now
from scrapeyard.runtime.background import BackgroundLoopMonitor
from scrapeyard.runtime.metrics import (
    CLEANUP_ARTIFACT_FINDINGS,
    CLEANUP_BYTES,
    CLEANUP_HISTORY_FAILURES,
    CLEANUP_ITEMS,
    CLEANUP_RUNS,
    mark_last_success,
)
from scrapeyard.storage.protocols import ErrorStore, JobStore, ResultStore, WebhookOutboxStore
from scrapeyard.storage.types import (
    DeletionFinalizationAction,
    DeletionReservationAction,
    ResultReconciliationReport,
)

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_HOURS = 6
_DEFAULT_CYCLE_MAX_ITEMS_PER_PHASE = 10_000
_DEFAULT_CYCLE_MAX_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class CleanupCycleOutcome:
    """Result used by the loop to choose normal cadence or prompt catch-up."""

    saturated: bool
    processed_items: int


class _CleanupCycleBudget:
    """Bound each phase independently while sharing one elapsed-time ceiling."""

    def __init__(
        self,
        *,
        max_items_per_phase: int,
        max_seconds: float,
        clock: Callable[[], float],
    ) -> None:
        if max_items_per_phase < 1:
            raise ValueError("Cleanup cycle item budget must be positive")
        if max_seconds <= 0:
            raise ValueError("Cleanup cycle time budget must be positive")
        self.max_items_per_phase = max_items_per_phase
        self.max_seconds = max_seconds
        self._clock = clock
        self._started = clock()
        self._phase_items: dict[str, int] = {}
        self.saturated = False

    @property
    def deadline(self) -> float:
        """Return the absolute monotonic deadline shared with storage work."""

        return self._started + self.max_seconds

    @property
    def processed_items(self) -> int:
        return sum(self._phase_items.values())

    def next_limit(self, phase: str, batch_size: int) -> int:
        if batch_size < 1:
            raise ValueError("Cleanup batch size must be positive")
        if self._clock() >= self.deadline:
            self.saturated = True
            return 0
        remaining = self.max_items_per_phase - self._phase_items.get(phase, 0)
        if remaining <= 0:
            self.saturated = True
            return 0
        return min(batch_size, remaining)

    def record(
        self,
        phase: str,
        count: int,
        *,
        limit: int,
        has_more: bool | None = None,
    ) -> bool:
        if count < 0 or count > limit:
            raise RuntimeError(
                f"Cleanup phase {phase!r} returned invalid count {count} for limit {limit}"
            )
        self._phase_items[phase] = self._phase_items.get(phase, 0) + count
        if count < limit:
            if has_more:
                raise RuntimeError(
                    f"Cleanup phase {phase!r} reported more work after a short page"
                )
            return False
        if has_more is False:
            return False
        if self.next_limit(phase, limit) == 0:
            self.saturated = True
            return False
        return True


async def _drain_count_phase(
    *,
    phase: str,
    batch_size: int,
    cycle: _CleanupCycleBudget,
    operation: Callable[[int], Awaitable[int]],
) -> int:
    total = 0
    while limit := cycle.next_limit(phase, batch_size):
        count = await operation(limit)
        total += count
        if not cycle.record(phase, count, limit=limit):
            break
    return total


async def _drain_artifact_reconciliation(
    *,
    result_store: ResultStore,
    grace_seconds: int,
    dry_run: bool,
    now: datetime | None,
    batch_size: int,
    cycle: _CleanupCycleBudget,
) -> ResultReconciliationReport:
    """Drain independent durable artifact cursors within their phase budgets."""

    aggregate = ResultReconciliationReport(dry_run=dry_run)
    metadata_pending = True
    filesystem_pending = True
    while metadata_pending or filesystem_pending:
        metadata_limit = (
            cycle.next_limit("artifact_metadata", batch_size)
            if metadata_pending
            else 0
        )
        filesystem_limit = (
            cycle.next_limit("artifact_filesystem", batch_size)
            if filesystem_pending
            else 0
        )
        if not metadata_limit and not filesystem_limit:
            break
        page = await result_store.reconcile_artifacts(
            grace_seconds=grace_seconds,
            dry_run=dry_run,
            now=now,
            batch_size=batch_size,
            metadata_scan_limit=metadata_limit,
            filesystem_scan_limit=filesystem_limit,
            deadline=cycle.deadline,
        )
        aggregate = aggregate.merged_with(
            page,
            metadata_scanned=bool(metadata_limit),
            filesystem_scanned=bool(filesystem_limit),
        )
        if metadata_limit:
            metadata_pending = not page.metadata_scan_exhausted
            cycle.record(
                "artifact_metadata",
                page.metadata_rows_inspected,
                limit=metadata_limit,
                has_more=metadata_pending,
            )
        if filesystem_limit:
            filesystem_pending = not page.filesystem_scan_exhausted
            cycle.record(
                "artifact_filesystem",
                page.filesystem_entries_inspected,
                limit=filesystem_limit,
                has_more=filesystem_pending,
            )
    return replace(
        aggregate,
        metadata_scan_exhausted=not metadata_pending,
        filesystem_scan_exhausted=not filesystem_pending,
    )


class CleanupIncompleteError(RuntimeError):
    """Raised after independent cleanup phases run when one or more failed."""

    def __init__(self, phases: list[str]) -> None:
        self.phases = tuple(phases)
        super().__init__(f"cleanup phases failed: {', '.join(phases)}")


@dataclass(frozen=True, slots=True)
class HistoryRetentionPolicy:
    """Bounded durable-history retention settings for one cleanup pass."""

    adhoc_job_retention_days: int
    scheduled_run_retention_days: int
    scheduled_run_retention_count: int
    error_retention_days: int
    webhook_tombstone_retention_days: int
    adhoc_job_batch_size: int
    scheduled_run_batch_size: int
    error_batch_size: int


def _history_failure(failed_phases: list[str], phase: str, exc: Exception) -> None:
    failed_phases.append(phase)
    CLEANUP_HISTORY_FAILURES.labels(phase).inc()
    logger.error(
        "Durable history cleanup failed phase=%s error_type=%s "
        "recovery_action=resume_next_cleanup_pass",
        phase,
        type(exc).__name__,
    )


async def _cleanup_durable_history(
    *,
    job_store: JobStore,
    error_store: ErrorStore,
    policy: HistoryRetentionPolicy,
    observed_at: datetime,
    cycle: _CleanupCycleBudget,
) -> list[str]:
    """Drain retry-safe history batches within per-phase cycle budgets."""

    failed_phases: list[str] = []
    tombstone_before = observed_at - timedelta(
        days=policy.webhook_tombstone_retention_days
    )
    try:
        errors_deleted = await _drain_count_phase(
            phase="expired_errors",
            batch_size=policy.error_batch_size,
            cycle=cycle,
            operation=lambda limit: error_store.delete_expired_errors(
                observed_at - timedelta(days=policy.error_retention_days),
                limit=limit,
            ),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _history_failure(failed_phases, "error_retention", exc)
    else:
        if errors_deleted:
            CLEANUP_ITEMS.labels("expired_errors").inc(errors_deleted)

    while adhoc_limit := cycle.next_limit(
        "adhoc_candidates",
        policy.adhoc_job_batch_size,
    ):
        try:
            adhoc_jobs = await job_store.list_adhoc_jobs_for_retention(
                observed_at - timedelta(days=policy.adhoc_job_retention_days),
                idempotency_observed_at=observed_at,
                tombstone_expired_before=tombstone_before,
                limit=adhoc_limit,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _history_failure(failed_phases, "adhoc_selection", exc)
            break
        selection_full = cycle.record(
            "adhoc_candidates",
            len(adhoc_jobs),
            limit=adhoc_limit,
        )
        retry_pending = False
        made_progress = False
        for job_id in adhoc_jobs:
            try:
                reservation = await job_store.reserve_job_deletion(
                    job_id,
                    delete_results=False,
                    requested_at=observed_at,
                )
                if reservation.action not in {
                    DeletionReservationAction.created,
                    DeletionReservationAction.resumed,
                }:
                    continue
                error_limit = cycle.next_limit(
                    "adhoc_job_errors",
                    policy.error_batch_size,
                )
                if not error_limit:
                    retry_pending = True
                    continue
                errors_deleted, errors_remain = (
                    await error_store.delete_errors_for_job_batch(
                        job_id,
                        limit=error_limit,
                    )
                )
                cycle.record(
                    "adhoc_job_errors",
                    errors_deleted,
                    limit=error_limit,
                )
                if errors_deleted:
                    made_progress = True
                    CLEANUP_ITEMS.labels("job_errors").inc(errors_deleted)
                if errors_remain:
                    retry_pending = True
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _history_failure(failed_phases, "adhoc_errors", exc)
                continue
            try:
                finalized = await job_store.finalize_job_deletion(
                    job_id,
                    delete_results=False,
                    preserve_idempotency_after=observed_at,
                )
                if (
                    finalized.action
                    is DeletionFinalizationAction.unexpired_idempotency_conflict
                ):
                    logger.info(
                        "Ad-hoc history deletion deferred for live idempotency reservation "
                        "job_id=%s recovery_action=retry_after_idempotency_expiry",
                        job_id,
                    )
                    continue
                if finalized.action not in {
                    DeletionFinalizationAction.deleted,
                    DeletionFinalizationAction.missing,
                }:
                    raise RuntimeError(
                        f"Ad-hoc history finalization returned {finalized.action.value}"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _history_failure(failed_phases, "adhoc_finalization", exc)
            else:
                made_progress = True
                if finalized.action is DeletionFinalizationAction.deleted:
                    CLEANUP_ITEMS.labels("adhoc_jobs").inc()
        if not selection_full and not (retry_pending and made_progress):
            break

    while scheduled_limit := cycle.next_limit(
        "scheduled_candidates",
        policy.scheduled_run_batch_size,
    ):
        try:
            scheduled_runs = await job_store.list_scheduled_runs_for_retention(
                observed_at - timedelta(days=policy.scheduled_run_retention_days),
                tombstone_expired_before=tombstone_before,
                max_runs_per_job=policy.scheduled_run_retention_count,
                limit=scheduled_limit,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _history_failure(failed_phases, "scheduled_selection", exc)
            break
        selection_full = cycle.record(
            "scheduled_candidates",
            len(scheduled_runs),
            limit=scheduled_limit,
        )
        retry_pending = False
        made_progress = False
        for job_id, run_id in scheduled_runs:
            try:
                error_limit = cycle.next_limit(
                    "scheduled_run_errors",
                    policy.error_batch_size,
                )
                if not error_limit:
                    retry_pending = True
                    continue
                errors_deleted, errors_remain = (
                    await error_store.delete_errors_for_run_batch(
                        run_id,
                        limit=error_limit,
                    )
                )
                cycle.record(
                    "scheduled_run_errors",
                    errors_deleted,
                    limit=error_limit,
                )
                if errors_deleted:
                    made_progress = True
                    CLEANUP_ITEMS.labels("run_errors").inc(errors_deleted)
                if errors_remain:
                    retry_pending = True
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _history_failure(failed_phases, "scheduled_errors", exc)
                continue
            try:
                pruned = await job_store.prune_scheduled_run_for_retention(
                    job_id,
                    run_id,
                    expired_before=(
                        observed_at
                        - timedelta(days=policy.scheduled_run_retention_days)
                    ),
                    tombstone_expired_before=tombstone_before,
                    max_runs_per_job=policy.scheduled_run_retention_count,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _history_failure(failed_phases, "scheduled_pruning", exc)
            else:
                made_progress = made_progress or pruned.pruned
                if pruned.pruned:
                    CLEANUP_ITEMS.labels("scheduled_runs").inc()
                    if pruned.webhook_tombstones_deleted:
                        CLEANUP_ITEMS.labels("webhook_tombstones").inc(
                            pruned.webhook_tombstones_deleted
                        )
        if not selection_full and not (retry_pending and made_progress):
            break
    return failed_phases


async def run_cleanup(
    result_store: ResultStore,
    retention_days: int,
    max_results_per_job: int,
    webhook_outbox_store: WebhookOutboxStore | None = None,
    webhook_delivered_retention_days: int | None = None,
    webhook_failed_retention_days: int | None = None,
    webhook_cleanup_batch_size: int = 100,
    result_cleanup_batch_size: int = 500,
    orphan_grace_seconds: int = 86400,
    reconciliation_dry_run: bool = True,
    job_store: JobStore | None = None,
    idempotency_cleanup_batch_size: int = 1000,
    error_store: ErrorStore | None = None,
    history_policy: HistoryRetentionPolicy | None = None,
    cycle_max_items_per_phase: int = _DEFAULT_CYCLE_MAX_ITEMS_PER_PHASE,
    cycle_max_seconds: float = _DEFAULT_CYCLE_MAX_SECONDS,
    *,
    now: datetime | None = None,
) -> CleanupCycleOutcome:
    """Drain bounded retention batches and report whether catch-up remains."""
    cycle = _CleanupCycleBudget(
        max_items_per_phase=cycle_max_items_per_phase,
        max_seconds=cycle_max_seconds,
        clock=time.monotonic,
    )
    failed_phases: list[str] = []
    try:
        deleted = await _drain_count_phase(
            phase="expired_results",
            batch_size=result_cleanup_batch_size,
            cycle=cycle,
            operation=lambda limit: result_store.delete_expired(
                retention_days,
                limit=limit,
            ),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        failed_phases.append("expired_result_retention")
        logger.error(
            "Expired result cleanup failed error_type=%s recovery_action=retry_next_cleanup_pass",
            type(exc).__name__,
        )
    else:
        if deleted:
            CLEANUP_ITEMS.labels("expired_results").inc(deleted)
            logger.info("Cleanup removed %d expired result(s)", deleted)

    try:
        pruned = await _drain_count_phase(
            phase="excess_results",
            batch_size=result_cleanup_batch_size,
            cycle=cycle,
            operation=lambda limit: result_store.prune_excess_per_job(
                max_results_per_job,
                limit=limit,
            ),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        failed_phases.append("per_job_result_retention")
        logger.error(
            "Per-job result cleanup failed error_type=%s recovery_action=retry_next_cleanup_pass",
            type(exc).__name__,
        )
    else:
        if pruned:
            CLEANUP_ITEMS.labels("excess_results").inc(pruned)
            logger.info("Cleanup pruned %d excess result(s) across jobs", pruned)

    try:
        reconciliation = await _drain_artifact_reconciliation(
            result_store=result_store,
            grace_seconds=orphan_grace_seconds,
            dry_run=reconciliation_dry_run,
            now=now,
            batch_size=result_cleanup_batch_size,
            cycle=cycle,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        failed_phases.append("artifact_reconciliation")
        logger.error(
            "Result artifact reconciliation failed error_type=%s "
            "recovery_action=retry_next_cleanup_pass",
            type(exc).__name__,
        )
    else:
        artifact_findings = {
            "missing": reconciliation.missing_result_files,
            "corrupt": reconciliation.corrupt_result_files,
            "unreadable": reconciliation.unreadable_result_files,
            "unsafe": reconciliation.unsafe_metadata_paths,
        }
        for kind, count in artifact_findings.items():
            if count:
                CLEANUP_ARTIFACT_FINDINGS.labels(kind).inc(count)
        if reconciliation.artifact_failures:
            logger.warning(
                "Result artifact validation found retained-result integrity failures "
                "missing=%s corrupt=%s unreadable=%s unsafe=%s "
                "recovery_action=restore_artifact_or_delete_metadata_or_accept_loss",
                artifact_findings["missing"],
                artifact_findings["corrupt"],
                artifact_findings["unreadable"],
                artifact_findings["unsafe"],
            )
        if reconciliation.operation_failures:
            failed_phases.append("artifact_reconciliation")
            logger.error(
                "Result artifact reconciliation completed with operation "
                "failures failure_count=%s recovery_action="
                "retry_failures_next_cleanup_pass",
                len(reconciliation.operation_failures),
            )
        if reconciliation.directories_removed:
            CLEANUP_ITEMS.labels("artifact_directories").inc(reconciliation.directories_removed)
        if reconciliation.files_removed:
            CLEANUP_ITEMS.labels("artifact_files").inc(reconciliation.files_removed)
        if reconciliation.metadata_rows_inspected:
            CLEANUP_ITEMS.labels("artifact_metadata_inspected").inc(
                reconciliation.metadata_rows_inspected
            )
        if reconciliation.filesystem_entries_inspected:
            CLEANUP_ITEMS.labels("artifact_filesystem_inspected").inc(
                reconciliation.filesystem_entries_inspected
            )
        if reconciliation.removed_bytes:
            CLEANUP_BYTES.inc(reconciliation.removed_bytes)
        logger.info(
            "Result artifact reconciliation complete dry_run=%s "
            "metadata_rows_inspected=%s valid_artifacts=%s "
            "missing_result_files=%s corrupt_result_files=%s "
            "unreadable_result_files=%s unsafe_metadata_paths=%s "
            "filesystem_entries_inspected=%s "
            "filesystem_run_directories_inspected=%s malformed_entries_ignored=%s "
            "orphan_candidates=%s "
            "recent_candidates_skipped=%s active_run_candidates_skipped=%s "
            "metadata_race_candidates_skipped=%s "
            "active_run_race_candidates_skipped=%s "
            "stale_temporary_candidates=%s directories_would_remove=%s "
            "files_would_remove=%s directories_removed=%s files_removed=%s "
            "removed_bytes=%s failure_count=%s artifact_error_types=%s "
            "operation_error_types=%s metadata_scan_exhausted=%s "
            "filesystem_scan_exhausted=%s has_more=%s "
            "recovery_action=retry_failures_next_cleanup_pass",
            reconciliation.dry_run,
            reconciliation.metadata_rows_inspected,
            reconciliation.valid_artifacts,
            reconciliation.missing_result_files,
            reconciliation.corrupt_result_files,
            reconciliation.unreadable_result_files,
            reconciliation.unsafe_metadata_paths,
            reconciliation.filesystem_entries_inspected,
            reconciliation.filesystem_run_directories_inspected,
            reconciliation.malformed_entries_ignored,
            reconciliation.orphan_candidates,
            reconciliation.recent_candidates_skipped,
            reconciliation.active_run_candidates_skipped,
            reconciliation.metadata_race_candidates_skipped,
            reconciliation.active_run_race_candidates_skipped,
            reconciliation.stale_temporary_candidates,
            reconciliation.directories_would_remove,
            reconciliation.files_would_remove,
            reconciliation.directories_removed,
            reconciliation.files_removed,
            reconciliation.removed_bytes,
            reconciliation.failure_count,
            ",".join(
                sorted(
                    {
                        failure.error_type
                        for failure in reconciliation.artifact_failures
                        if failure.error_type is not None
                    }
                )
            )
            or "none",
            ",".join(sorted({failure.error_type for failure in reconciliation.operation_failures}))
            or "none",
            reconciliation.metadata_scan_exhausted,
            reconciliation.filesystem_scan_exhausted,
            reconciliation.has_more,
        )

    observed_at = now or utc_now()
    if job_store is not None:
        try:
            idempotency_deleted = await _drain_count_phase(
                phase="idempotency_records",
                batch_size=idempotency_cleanup_batch_size,
                cycle=cycle,
                operation=lambda limit: job_store.delete_expired_idempotency_records(
                    observed_at,
                    limit=limit,
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failed_phases.append("idempotency_retention")
            logger.error(
                "Idempotency retention cleanup failed error_type=%s "
                "recovery_action=retry_next_cleanup_pass",
                type(exc).__name__,
            )
        else:
            if idempotency_deleted:
                CLEANUP_ITEMS.labels("idempotency_records").inc(idempotency_deleted)
            logger.info(
                "Idempotency retention cleanup complete deleted_count=%s batch_limit=%s",
                idempotency_deleted,
                idempotency_cleanup_batch_size,
            )

    if webhook_outbox_store is not None:
        if webhook_delivered_retention_days is None or webhook_failed_retention_days is None:
            raise ValueError("Webhook retention windows are required for outbox cleanup")

        delivered_scrubbed = 0
        failed_scrubbed = 0
        try:
            while webhook_limit := cycle.next_limit(
                "webhook_secrets",
                webhook_cleanup_batch_size,
            ):
                summary = await webhook_outbox_store.scrub_terminal_deliveries(
                    delivered_before=(
                        observed_at - timedelta(days=webhook_delivered_retention_days)
                    ),
                    failed_before=(
                        observed_at - timedelta(days=webhook_failed_retention_days)
                    ),
                    scrubbed_at=observed_at,
                    limit=webhook_limit,
                )
                scrubbed = summary.delivered_scrubbed + summary.failed_scrubbed
                delivered_scrubbed += summary.delivered_scrubbed
                failed_scrubbed += summary.failed_scrubbed
                if not cycle.record(
                    "webhook_secrets",
                    scrubbed,
                    limit=webhook_limit,
                ):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failed_phases.append("webhook_retention")
            logger.error(
                "Webhook retention cleanup failed error_type=%s "
                "recovery_action=retry_next_cleanup_pass",
                type(exc).__name__,
            )
        else:
            logger.info(
                "Webhook retention cleanup complete delivered_scrubbed_count=%s "
                "failed_scrubbed_count=%s batch_limit=%s "
                "recovery_action=retain_terminal_tombstones",
                delivered_scrubbed,
                failed_scrubbed,
                webhook_cleanup_batch_size,
            )
            scrubbed = delivered_scrubbed + failed_scrubbed
            if scrubbed:
                CLEANUP_ITEMS.labels("webhook_secrets").inc(scrubbed)

    if (error_store is None) is not (history_policy is None):
        raise ValueError("History retention requires both error_store and history_policy")
    if job_store is not None and error_store is not None and history_policy is not None:
        failed_phases.extend(
            await _cleanup_durable_history(
                job_store=job_store,
                error_store=error_store,
                policy=history_policy,
                observed_at=observed_at,
                cycle=cycle,
            )
        )

    if failed_phases:
        raise CleanupIncompleteError(failed_phases)
    return CleanupCycleOutcome(
        saturated=cycle.saturated,
        processed_items=cycle.processed_items,
    )


def start_cleanup_loop(
    result_store: ResultStore,
    webhook_outbox_store: WebhookOutboxStore | None = None,
    interval_hours: float = _DEFAULT_INTERVAL_HOURS,
    *,
    job_store: JobStore | None = None,
    error_store: ErrorStore | None = None,
    monitor: BackgroundLoopMonitor | None = None,
) -> asyncio.Task[None]:
    """Spawn a background task that periodically runs cleanup.

    Reads settings from :func:`get_settings` on each iteration and delegates all
    storage work to the configured result store.

    Returns the :class:`asyncio.Task` so the caller can cancel it on shutdown.
    """

    async def _loop() -> None:
        while True:
            sleep_seconds = interval_hours * 3600
            try:
                settings = get_settings()
                history_policy = HistoryRetentionPolicy(
                    adhoc_job_retention_days=(settings.history_adhoc_job_retention_days),
                    scheduled_run_retention_days=(
                        settings.history_scheduled_run_retention_days
                    ),
                    scheduled_run_retention_count=(
                        settings.history_scheduled_run_retention_count
                    ),
                    error_retention_days=settings.history_error_retention_days,
                    webhook_tombstone_retention_days=(
                        settings.history_webhook_tombstone_retention_days
                    ),
                    adhoc_job_batch_size=(
                        settings.history_adhoc_job_cleanup_batch_size
                    ),
                    scheduled_run_batch_size=(
                        settings.history_scheduled_run_cleanup_batch_size
                    ),
                    error_batch_size=settings.history_error_cleanup_batch_size,
                ) if job_store is not None and error_store is not None else None
                if webhook_outbox_store is None:
                    outcome = await run_cleanup(
                        result_store=result_store,
                        retention_days=settings.storage_retention_days,
                        max_results_per_job=settings.storage_max_results_per_job,
                        result_cleanup_batch_size=settings.storage_cleanup_batch_size,
                        orphan_grace_seconds=settings.storage_orphan_grace_seconds,
                        reconciliation_dry_run=(settings.storage_reconciliation_dry_run),
                        job_store=job_store,
                        idempotency_cleanup_batch_size=(settings.idempotency_cleanup_batch_size)
                        if job_store is not None
                        else 1000,
                        error_store=error_store,
                        history_policy=history_policy,
                        cycle_max_items_per_phase=(
                            getattr(
                                settings,
                                "storage_cleanup_cycle_max_items_per_phase",
                                _DEFAULT_CYCLE_MAX_ITEMS_PER_PHASE,
                            )
                        ),
                        cycle_max_seconds=getattr(
                            settings,
                            "storage_cleanup_cycle_max_seconds",
                            _DEFAULT_CYCLE_MAX_SECONDS,
                        ),
                    )
                else:
                    outcome = await run_cleanup(
                        result_store=result_store,
                        retention_days=settings.storage_retention_days,
                        max_results_per_job=settings.storage_max_results_per_job,
                        result_cleanup_batch_size=settings.storage_cleanup_batch_size,
                        webhook_outbox_store=webhook_outbox_store,
                        webhook_delivered_retention_days=(
                            settings.webhook_delivered_retention_days
                        ),
                        webhook_failed_retention_days=(settings.webhook_failed_retention_days),
                        webhook_cleanup_batch_size=(settings.webhook_dispatch_batch_size),
                        orphan_grace_seconds=settings.storage_orphan_grace_seconds,
                        reconciliation_dry_run=(settings.storage_reconciliation_dry_run),
                        job_store=job_store,
                        idempotency_cleanup_batch_size=(settings.idempotency_cleanup_batch_size)
                        if job_store is not None
                        else 1000,
                        error_store=error_store,
                        history_policy=history_policy,
                        cycle_max_items_per_phase=(
                            getattr(
                                settings,
                                "storage_cleanup_cycle_max_items_per_phase",
                                _DEFAULT_CYCLE_MAX_ITEMS_PER_PHASE,
                            )
                        ),
                        cycle_max_seconds=getattr(
                            settings,
                            "storage_cleanup_cycle_max_seconds",
                            _DEFAULT_CYCLE_MAX_SECONDS,
                        ),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                CLEANUP_RUNS.labels("failed").inc()
                if monitor is not None:
                    monitor.record_failure(exc)
                logger.exception("Error during result cleanup")
            else:
                CLEANUP_RUNS.labels("success").inc()
                if monitor is not None:
                    monitor.record_success()
                mark_last_success("cleanup")
                if isinstance(outcome, CleanupCycleOutcome) and outcome.saturated:
                    sleep_seconds = getattr(
                        settings,
                        "storage_cleanup_catchup_delay_seconds",
                        5.0,
                    )
                    logger.info(
                        "Cleanup cycle reached its bounded catch-up budget "
                        "processed_items=%s retry_delay_seconds=%s",
                        outcome.processed_items,
                        sleep_seconds,
                    )
            await asyncio.sleep(sleep_seconds)

    task = asyncio.create_task(_loop(), name="result-cleanup")
    if monitor is not None:
        monitor.bind(task)
    return task
