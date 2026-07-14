"""Periodic cleanup of expired scrape results."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from scrapeyard.common.settings import get_settings
from scrapeyard.common.time import utc_now
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
)

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_HOURS = 6


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
) -> list[str]:
    """Run bounded, retry-safe cleanup across jobs.db and errors.db."""

    failed_phases: list[str] = []
    tombstone_before = observed_at - timedelta(
        days=policy.webhook_tombstone_retention_days
    )
    try:
        errors_deleted = await error_store.delete_expired_errors(
            observed_at - timedelta(days=policy.error_retention_days),
            limit=policy.error_batch_size,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _history_failure(failed_phases, "error_retention", exc)
    else:
        if errors_deleted:
            CLEANUP_ITEMS.labels("expired_errors").inc(errors_deleted)

    try:
        adhoc_jobs = await job_store.list_adhoc_jobs_for_retention(
            observed_at - timedelta(days=policy.adhoc_job_retention_days),
            idempotency_observed_at=observed_at,
            tombstone_expired_before=tombstone_before,
            limit=policy.adhoc_job_batch_size,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _history_failure(failed_phases, "adhoc_selection", exc)
        adhoc_jobs = []

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
            errors_deleted, errors_remain = await error_store.delete_errors_for_job_batch(
                job_id,
                limit=policy.error_batch_size,
            )
            if errors_deleted:
                CLEANUP_ITEMS.labels("job_errors").inc(errors_deleted)
            if errors_remain:
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
            if finalized.action is DeletionFinalizationAction.deleted:
                CLEANUP_ITEMS.labels("adhoc_jobs").inc()

    try:
        scheduled_runs = await job_store.list_scheduled_runs_for_retention(
            observed_at - timedelta(days=policy.scheduled_run_retention_days),
            tombstone_expired_before=tombstone_before,
            max_runs_per_job=policy.scheduled_run_retention_count,
            limit=policy.scheduled_run_batch_size,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _history_failure(failed_phases, "scheduled_selection", exc)
        scheduled_runs = []

    for job_id, run_id in scheduled_runs:
        try:
            errors_deleted, errors_remain = await error_store.delete_errors_for_run_batch(
                run_id,
                limit=policy.error_batch_size,
            )
            if errors_deleted:
                CLEANUP_ITEMS.labels("run_errors").inc(errors_deleted)
            if errors_remain:
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
                    observed_at - timedelta(days=policy.scheduled_run_retention_days)
                ),
                tombstone_expired_before=tombstone_before,
                max_runs_per_job=policy.scheduled_run_retention_count,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _history_failure(failed_phases, "scheduled_pruning", exc)
        else:
            if pruned.pruned:
                CLEANUP_ITEMS.labels("scheduled_runs").inc()
                if pruned.webhook_tombstones_deleted:
                    CLEANUP_ITEMS.labels("webhook_tombstones").inc(
                        pruned.webhook_tombstones_deleted
                    )
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
    *,
    now: datetime | None = None,
) -> None:
    """Clean result artifacts and scrub bounded terminal webhook secrets."""
    failed_phases: list[str] = []
    try:
        deleted = await result_store.delete_expired(
            retention_days,
            limit=result_cleanup_batch_size,
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
        pruned = await result_store.prune_excess_per_job(
            max_results_per_job,
            limit=result_cleanup_batch_size,
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
        reconciliation = await result_store.reconcile_artifacts(
            grace_seconds=orphan_grace_seconds,
            dry_run=reconciliation_dry_run,
            now=now,
            batch_size=result_cleanup_batch_size,
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
        if reconciliation.removed_bytes:
            CLEANUP_BYTES.inc(reconciliation.removed_bytes)
        logger.info(
            "Result artifact reconciliation complete dry_run=%s "
            "metadata_rows_inspected=%s valid_artifacts=%s "
            "missing_result_files=%s corrupt_result_files=%s "
            "unreadable_result_files=%s unsafe_metadata_paths=%s "
            "filesystem_run_directories_inspected=%s malformed_entries_ignored=%s "
            "orphan_candidates=%s "
            "recent_candidates_skipped=%s active_run_candidates_skipped=%s "
            "metadata_race_candidates_skipped=%s "
            "active_run_race_candidates_skipped=%s "
            "stale_temporary_candidates=%s directories_would_remove=%s "
            "files_would_remove=%s directories_removed=%s files_removed=%s "
            "removed_bytes=%s failure_count=%s artifact_error_types=%s "
            "operation_error_types=%s "
            "recovery_action=retry_failures_next_cleanup_pass",
            reconciliation.dry_run,
            reconciliation.metadata_rows_inspected,
            reconciliation.valid_artifacts,
            reconciliation.missing_result_files,
            reconciliation.corrupt_result_files,
            reconciliation.unreadable_result_files,
            reconciliation.unsafe_metadata_paths,
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
        )

    observed_at = now or utc_now()
    if job_store is not None:
        try:
            idempotency_deleted = await job_store.delete_expired_idempotency_records(
                observed_at,
                limit=idempotency_cleanup_batch_size,
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

        try:
            summary = await webhook_outbox_store.scrub_terminal_deliveries(
                delivered_before=observed_at - timedelta(days=webhook_delivered_retention_days),
                failed_before=observed_at - timedelta(days=webhook_failed_retention_days),
                scrubbed_at=observed_at,
                limit=webhook_cleanup_batch_size,
            )
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
                summary.delivered_scrubbed,
                summary.failed_scrubbed,
                webhook_cleanup_batch_size,
            )
            scrubbed = summary.delivered_scrubbed + summary.failed_scrubbed
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
            )
        )

    if failed_phases:
        raise CleanupIncompleteError(failed_phases)


def start_cleanup_loop(
    result_store: ResultStore,
    webhook_outbox_store: WebhookOutboxStore | None = None,
    interval_hours: float = _DEFAULT_INTERVAL_HOURS,
    *,
    job_store: JobStore | None = None,
    error_store: ErrorStore | None = None,
) -> asyncio.Task[None]:
    """Spawn a background task that periodically runs cleanup.

    Reads settings from :func:`get_settings` on each iteration and delegates all
    storage work to the configured result store.

    Returns the :class:`asyncio.Task` so the caller can cancel it on shutdown.
    """

    async def _loop() -> None:
        while True:
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
                    await run_cleanup(
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
                    )
                else:
                    await run_cleanup(
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
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                CLEANUP_RUNS.labels("failed").inc()
                logger.exception("Error during result cleanup")
            else:
                CLEANUP_RUNS.labels("success").inc()
                mark_last_success("cleanup")
            await asyncio.sleep(interval_hours * 3600)

    return asyncio.create_task(_loop(), name="result-cleanup")
