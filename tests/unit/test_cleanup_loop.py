"""Test the result retention cleanup loop."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from scrapeyard.storage.cleanup import start_cleanup_loop


@pytest.mark.asyncio
async def test_cleanup_loop_runs_periodically():
    mock_run_cleanup = AsyncMock()

    with patch("scrapeyard.storage.cleanup.run_cleanup", mock_run_cleanup), \
         patch("scrapeyard.storage.cleanup.get_db") as mock_get_db:
        # Make get_db return an async context manager yielding a mock connection.
        mock_conn = AsyncMock()
        mock_get_db.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_get_db.return_value.__aexit__ = AsyncMock(return_value=False)

        task = start_cleanup_loop(interval_hours=0.05 / 3600)
        await asyncio.sleep(0.15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert mock_run_cleanup.call_count >= 2


@pytest.mark.asyncio
async def test_cleanup_loop_handles_cancellation():
    mock_run_cleanup = AsyncMock()

    with patch("scrapeyard.storage.cleanup.run_cleanup", mock_run_cleanup), \
         patch("scrapeyard.storage.cleanup.get_db") as mock_get_db:
        mock_conn = AsyncMock()
        mock_get_db.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_get_db.return_value.__aexit__ = AsyncMock(return_value=False)

        task = start_cleanup_loop(interval_hours=0.05 / 3600)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
