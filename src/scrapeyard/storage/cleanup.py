"""Periodic cleanup of expired scrape results."""

from __future__ import annotations

import asyncio
import logging

from scrapeyard.common.settings import get_settings
from scrapeyard.storage.protocols import ResultStore

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_HOURS = 6


async def run_cleanup(
    result_store: ResultStore,
    retention_days: int,
    max_results_per_job: int,
) -> None:
    """Remove expired result files and prune runs exceeding the per-job limit."""
    deleted = await result_store.delete_expired(retention_days)
    if deleted:
        logger.info("Cleanup removed %d expired result(s)", deleted)

    pruned = await result_store.prune_excess_per_job(max_results_per_job)
    if pruned:
        logger.info("Cleanup pruned %d excess result(s) across jobs", pruned)


def start_cleanup_loop(
    result_store: ResultStore,
    interval_hours: float = _DEFAULT_INTERVAL_HOURS,
) -> asyncio.Task:
    """Spawn a background task that periodically runs cleanup.

    Reads settings from :func:`get_settings` on each iteration and delegates all
    storage work to the configured result store.

    Returns the :class:`asyncio.Task` so the caller can cancel it on shutdown.
    """
    settings = get_settings()

    async def _loop() -> None:
        while True:
            try:
                await run_cleanup(
                    result_store=result_store,
                    retention_days=settings.storage_retention_days,
                    max_results_per_job=settings.storage_max_results_per_job,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error during result cleanup")
            await asyncio.sleep(interval_hours * 3600)

    return asyncio.create_task(_loop(), name="result-cleanup")
