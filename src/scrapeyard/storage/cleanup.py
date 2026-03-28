"""Periodic cleanup of expired scrape results."""

from __future__ import annotations

import asyncio
import logging

import aiosqlite

from scrapeyard.common.settings import get_settings
from scrapeyard.storage.database import get_db
from scrapeyard.storage.filesystem import remove_directories
from scrapeyard.storage.protocols import ResultStore

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_HOURS = 6


async def run_cleanup(
    result_store: ResultStore,
    retention_days: int,
    max_results_per_job: int,
    db: aiosqlite.Connection,
) -> None:
    """Remove expired result files and prune runs exceeding the per-job limit.

    Delegates age-based retention to the result store and keeps per-job pruning
    local to the cleanup loop.

    Parameters
    ----------
    result_store:
        Store implementation responsible for age-based result deletion.
    retention_days:
        Delete results older than this many days.
    max_results_per_job:
        Maximum number of result runs to keep per job (most recent kept).
    db:
        An open ``aiosqlite.Connection`` to ``results_meta.db``.
    """
    deleted = await result_store.delete_expired(retention_days)
    if deleted:
        logger.info("Cleanup removed %d expired result(s)", deleted)

    # 2. Per-job pruning: keep only max_results_per_job most recent runs per job.
    cursor = await db.execute(
        """
        SELECT id, file_path FROM (
            SELECT id, file_path,
                   ROW_NUMBER() OVER (PARTITION BY job_id ORDER BY created_at DESC) AS rn
            FROM results_meta
        ) WHERE rn > ?
        """,
        (max_results_per_job,),
    )
    excess_rows = await cursor.fetchall()

    if excess_rows:
        await asyncio.to_thread(
            remove_directories,
            [file_path for _, file_path in excess_rows],
        )
        ids = [r[0] for r in excess_rows]
        placeholders = ",".join("?" for _ in ids)
        await db.execute(
            f"DELETE FROM results_meta WHERE id IN ({placeholders})",
            ids,
        )
        await db.commit()
        logger.info("Cleanup pruned %d excess result(s) across jobs", len(excess_rows))


def start_cleanup_loop(
    result_store: ResultStore,
    interval_hours: float = _DEFAULT_INTERVAL_HOURS,
) -> asyncio.Task:
    """Spawn a background task that periodically runs cleanup.

    Reads settings from :func:`get_settings` and obtains a database
    connection via :func:`get_db` on each iteration.

    Returns the :class:`asyncio.Task` so the caller can cancel it on shutdown.
    """
    settings = get_settings()

    async def _loop() -> None:
        while True:
            try:
                async with get_db("results_meta.db") as db:
                    await run_cleanup(
                        result_store=result_store,
                        retention_days=settings.storage_retention_days,
                        max_results_per_job=settings.storage_max_results_per_job,
                        db=db,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error during result cleanup")
            await asyncio.sleep(interval_hours * 3600)

    return asyncio.create_task(_loop(), name="result-cleanup")
