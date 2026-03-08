"""Test the result retention cleanup loop."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from scrapeyard.storage.cleanup import start_cleanup_loop


@pytest.mark.asyncio
async def test_cleanup_loop_calls_delete_expired():
    mock_store = AsyncMock()
    mock_store.delete_expired = AsyncMock(return_value=0)

    task = start_cleanup_loop(mock_store, retention_days=30, interval_seconds=0.05)
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert mock_store.delete_expired.call_count >= 2
    mock_store.delete_expired.assert_called_with(30)


@pytest.mark.asyncio
async def test_cleanup_loop_handles_cancellation():
    mock_store = AsyncMock()
    mock_store.delete_expired = AsyncMock(return_value=0)

    task = start_cleanup_loop(mock_store, retention_days=7, interval_seconds=0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
