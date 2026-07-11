"""Periodic cleanup of expired scrape results."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from scrapeyard.common.settings import get_settings
from scrapeyard.common.time import utc_now
from scrapeyard.storage.protocols import ResultStore, WebhookOutboxStore

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_HOURS = 6


async def run_cleanup(
    result_store: ResultStore,
    retention_days: int,
    max_results_per_job: int,
    webhook_outbox_store: WebhookOutboxStore | None = None,
    webhook_delivered_retention_days: int | None = None,
    webhook_failed_retention_days: int | None = None,
    webhook_cleanup_batch_size: int = 100,
    orphan_grace_seconds: int = 86400,
    reconciliation_dry_run: bool = True,
    *,
    now: datetime | None = None,
) -> None:
    """Clean result artifacts and scrub bounded terminal webhook secrets."""
    deleted = await result_store.delete_expired(retention_days)
    if deleted:
        logger.info("Cleanup removed %d expired result(s)", deleted)

    pruned = await result_store.prune_excess_per_job(max_results_per_job)
    if pruned:
        logger.info("Cleanup pruned %d excess result(s) across jobs", pruned)

    try:
        reconciliation = await result_store.reconcile_artifacts(
            grace_seconds=orphan_grace_seconds,
            dry_run=reconciliation_dry_run,
            now=now,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error(
            "Result artifact reconciliation failed error_type=%s "
            "recovery_action=retry_next_cleanup_pass",
            type(exc).__name__,
        )
    else:
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
            ",".join(
                sorted(
                    {
                        failure.error_type
                        for failure in reconciliation.operation_failures
                    }
                )
            )
            or "none",
        )

    if webhook_outbox_store is None:
        return
    if (
        webhook_delivered_retention_days is None
        or webhook_failed_retention_days is None
    ):
        raise ValueError("Webhook retention windows are required for outbox cleanup")

    observed_at = now or utc_now()
    try:
        summary = await webhook_outbox_store.scrub_terminal_deliveries(
            delivered_before=observed_at
            - timedelta(days=webhook_delivered_retention_days),
            failed_before=observed_at - timedelta(days=webhook_failed_retention_days),
            scrubbed_at=observed_at,
            limit=webhook_cleanup_batch_size,
        )
    except Exception as exc:
        logger.error(
            "Webhook retention cleanup failed error_type=%s "
            "recovery_action=retry_next_cleanup_pass",
            type(exc).__name__,
        )
        return
    logger.info(
        "Webhook retention cleanup complete delivered_scrubbed_count=%s "
        "failed_scrubbed_count=%s batch_limit=%s "
        "recovery_action=retain_terminal_tombstones",
        summary.delivered_scrubbed,
        summary.failed_scrubbed,
        webhook_cleanup_batch_size,
    )


def start_cleanup_loop(
    result_store: ResultStore,
    webhook_outbox_store: WebhookOutboxStore | None = None,
    interval_hours: float = _DEFAULT_INTERVAL_HOURS,
) -> asyncio.Task:
    """Spawn a background task that periodically runs cleanup.

    Reads settings from :func:`get_settings` on each iteration and delegates all
    storage work to the configured result store.

    Returns the :class:`asyncio.Task` so the caller can cancel it on shutdown.
    """
    async def _loop() -> None:
        while True:
            try:
                settings = get_settings()
                if webhook_outbox_store is None:
                    await run_cleanup(
                        result_store=result_store,
                        retention_days=settings.storage_retention_days,
                        max_results_per_job=settings.storage_max_results_per_job,
                        orphan_grace_seconds=settings.storage_orphan_grace_seconds,
                        reconciliation_dry_run=(
                            settings.storage_reconciliation_dry_run
                        ),
                    )
                else:
                    await run_cleanup(
                        result_store=result_store,
                        retention_days=settings.storage_retention_days,
                        max_results_per_job=settings.storage_max_results_per_job,
                        webhook_outbox_store=webhook_outbox_store,
                        webhook_delivered_retention_days=(
                            settings.webhook_delivered_retention_days
                        ),
                        webhook_failed_retention_days=(
                            settings.webhook_failed_retention_days
                        ),
                        webhook_cleanup_batch_size=(
                            settings.webhook_dispatch_batch_size
                        ),
                        orphan_grace_seconds=settings.storage_orphan_grace_seconds,
                        reconciliation_dry_run=(
                            settings.storage_reconciliation_dry_run
                        ),
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error during result cleanup")
            await asyncio.sleep(interval_hours * 3600)

    return asyncio.create_task(_loop(), name="result-cleanup")
