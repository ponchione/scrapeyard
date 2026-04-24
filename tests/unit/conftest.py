"""Shared fixtures for unit tests."""

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def mock_stores():
    """Return (job_store, result_store, error_store, circuit_breaker) mocks.

    Common fixture used by worker test modules that exercise scrape_task().
    """
    job_store = AsyncMock()
    result_store = AsyncMock()
    error_store = AsyncMock()
    circuit_breaker = MagicMock()
    circuit_breaker.check = MagicMock()
    circuit_breaker.record_success = MagicMock()
    circuit_breaker.record_failure = MagicMock()
    return job_store, result_store, error_store, circuit_breaker
