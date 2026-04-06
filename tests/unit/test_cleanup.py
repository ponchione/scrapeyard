"""Test result retention auto-cleanup."""

from unittest.mock import AsyncMock

import pytest

from scrapeyard.storage.cleanup import run_cleanup


@pytest.mark.asyncio
async def test_run_cleanup_delegates_age_based_deletion():
    result_store = AsyncMock()
    result_store.delete_expired = AsyncMock(return_value=1)
    result_store.prune_excess_per_job = AsyncMock(return_value=0)

    await run_cleanup(result_store, retention_days=30, max_results_per_job=100)

    result_store.delete_expired.assert_awaited_once_with(30)
    result_store.prune_excess_per_job.assert_awaited_once_with(100)


@pytest.mark.asyncio
async def test_run_cleanup_delegates_per_job_pruning():
    result_store = AsyncMock()
    result_store.delete_expired = AsyncMock(return_value=0)
    result_store.prune_excess_per_job = AsyncMock(return_value=2)

    await run_cleanup(result_store, retention_days=14, max_results_per_job=3)

    result_store.delete_expired.assert_awaited_once_with(14)
    result_store.prune_excess_per_job.assert_awaited_once_with(3)
