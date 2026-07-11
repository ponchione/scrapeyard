"""Shared fixtures for unit tests."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from scrapeyard.storage.types import SaveResultMeta


@pytest.fixture
def mock_stores():
    """Return (job_store, result_store, error_store, circuit_breaker) mocks.

    Common fixture used by worker test modules that exercise scrape_task().
    """
    job_store = AsyncMock()
    job_store.claim_run.return_value = True
    job_store.queue_run.return_value = True
    job_store.run_is_active.return_value = True
    result_store = AsyncMock()
    result_store.save_result.return_value = SaveResultMeta(
        run_id="run-result",
        file_path="/tmp/results/run-result",
        record_count=0,
        serialized_bytes=128,
    )
    error_store = AsyncMock()
    error_store.count_errors_for_run.return_value = 0
    circuit_breaker = MagicMock()
    circuit_breaker.check = MagicMock()
    circuit_breaker.record_success = MagicMock()
    circuit_breaker.record_failure = MagicMock()
    return job_store, result_store, error_store, circuit_breaker
