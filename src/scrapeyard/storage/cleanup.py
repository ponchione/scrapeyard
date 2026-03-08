"""Periodic cleanup of expired scrape results."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scrapeyard.storage.result_store import LocalResultStore

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL = 3600  # 1 hour


def start_cleanup_loop(
    result_store: LocalResultStore,
    retention_days: int,
    interval_seconds: float = _DEFAULT_INTERVAL,
) -> asyncio.Task:
    """Spawn a background task that periodically deletes expired results.

    Returns the :class:`asyncio.Task` so the caller can cancel it on shutdown.
    """

    async def _loop() -> None:
        while True:
            try:
                deleted = await result_store.delete_expired(retention_days)
                if deleted:
                    logger.info("Cleanup removed %d expired result(s)", deleted)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error during result cleanup")
            await asyncio.sleep(interval_seconds)

    return asyncio.create_task(_loop(), name="result-cleanup")
