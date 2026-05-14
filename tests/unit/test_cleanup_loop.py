"""Test the result retention cleanup loop."""

import asyncio
from types import SimpleNamespace
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


@pytest.mark.asyncio
async def test_cleanup_loop_reads_settings_each_iteration(monkeypatch):
    calls = []
    settings = iter([
        SimpleNamespace(storage_retention_days=7, storage_max_results_per_job=20),
        SimpleNamespace(storage_retention_days=14, storage_max_results_per_job=40),
    ])

    async def fake_run_cleanup(**kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr("scrapeyard.storage.cleanup.get_settings", lambda: next(settings))
    monkeypatch.setattr("scrapeyard.storage.cleanup.run_cleanup", fake_run_cleanup)

    task = start_cleanup_loop(MagicMock(), interval_hours=0)
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [call["retention_days"] for call in calls] == [7, 14]
    assert [call["max_results_per_job"] for call in calls] == [20, 40]
