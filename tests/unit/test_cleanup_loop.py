"""Test the result retention cleanup loop."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scrapeyard.storage.cleanup import start_cleanup_loop


@pytest.mark.asyncio
async def test_cleanup_loop_runs_periodically():
    mock_run_cleanup = AsyncMock()
    mock_result_store = MagicMock()

    with patch("scrapeyard.storage.cleanup.run_cleanup", mock_run_cleanup):
        task = start_cleanup_loop(mock_result_store, interval_hours=0.05 / 3600)
        await asyncio.sleep(0.15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert mock_run_cleanup.call_count >= 2
    assert all(
        call.kwargs["result_store"] is mock_result_store
        for call in mock_run_cleanup.await_args_list
    )


@pytest.mark.asyncio
async def test_cleanup_loop_handles_cancellation():
    mock_run_cleanup = AsyncMock()
    mock_result_store = MagicMock()

    with patch("scrapeyard.storage.cleanup.run_cleanup", mock_run_cleanup):
        task = start_cleanup_loop(mock_result_store, interval_hours=0.05 / 3600)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
