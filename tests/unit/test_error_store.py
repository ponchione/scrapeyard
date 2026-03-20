"""Tests for SQLiteErrorStore insert and filtered queries."""

from __future__ import annotations

from datetime import datetime

import pytest

from scrapeyard.models.job import ActionTaken, ErrorFilters, ErrorRecord, ErrorType
from scrapeyard.storage.database import init_db
from scrapeyard.storage.error_store import SQLiteErrorStore


@pytest.fixture()
async def store(tmp_path):
    await init_db(str(tmp_path / "db"))
    return SQLiteErrorStore()


def _make_error(**overrides) -> ErrorRecord:
    defaults = {
        "job_id": "j-1",
        "run_id": "run-1",
        "project": "acme",
        "target_url": "https://example.com",
        "attempt": 1,
        "timestamp": datetime(2026, 3, 1, 12, 0, 0),
        "error_type": ErrorType.http_error,
        "http_status": 503,
        "fetcher_used": "httpx",
        "error_message": "HTTP 503",
        "selectors_matched": {"h1": 1},
        "action_taken": ActionTaken.retry,
    }
    defaults.update(overrides)
    return ErrorRecord(**defaults)


async def test_log_and_query_all(store):
    err = _make_error()
    await store.log_error(err)

    results = await store.query_errors(ErrorFilters())
    assert len(results) == 1
    r = results[0]
    assert r.job_id == "j-1"
    assert r.project == "acme"
    assert r.error_type == ErrorType.http_error
    assert r.http_status == 503
    assert r.error_message == "HTTP 503"
    assert r.selectors_matched == {"h1": 1}
    assert r.action_taken == ActionTaken.retry
    assert r.resolved is False


async def test_log_error_null_optionals(store):
    err = _make_error(http_status=None, error_message=None, selectors_matched=None)
    await store.log_error(err)

    results = await store.query_errors(ErrorFilters())
    assert len(results) == 1
    assert results[0].http_status is None
    assert results[0].error_message is None
    assert results[0].selectors_matched is None


async def test_filter_by_project(store):
    await store.log_error(_make_error(project="acme", job_id="j-1"))
    await store.log_error(_make_error(project="other", job_id="j-2"))

    results = await store.query_errors(ErrorFilters(project="acme"))
    assert len(results) == 1
    assert results[0].project == "acme"


async def test_filter_by_job_id(store):
    await store.log_error(_make_error(job_id="j-1"))
    await store.log_error(_make_error(job_id="j-2"))

    results = await store.query_errors(ErrorFilters(job_id="j-2"))
    assert len(results) == 1
    assert results[0].job_id == "j-2"


async def test_filter_by_since(store):
    await store.log_error(_make_error(timestamp=datetime(2026, 1, 1)))
    await store.log_error(_make_error(timestamp=datetime(2026, 6, 1)))

    results = await store.query_errors(ErrorFilters(since=datetime(2026, 3, 1)))
    assert len(results) == 1
    assert results[0].timestamp == datetime(2026, 6, 1)


async def test_filter_by_error_type(store):
    await store.log_error(_make_error(error_type=ErrorType.http_error))
    await store.log_error(_make_error(error_type=ErrorType.timeout))

    results = await store.query_errors(ErrorFilters(error_type=ErrorType.timeout))
    assert len(results) == 1
    assert results[0].error_type == ErrorType.timeout


async def test_combined_filters(store):
    await store.log_error(_make_error(project="acme", job_id="j-1", error_type=ErrorType.http_error))
    await store.log_error(_make_error(project="acme", job_id="j-1", error_type=ErrorType.timeout))
    await store.log_error(_make_error(project="acme", job_id="j-2", error_type=ErrorType.http_error))
    await store.log_error(_make_error(project="other", job_id="j-3", error_type=ErrorType.http_error))

    results = await store.query_errors(ErrorFilters(project="acme", job_id="j-1", error_type=ErrorType.http_error))
    assert len(results) == 1
    assert results[0].job_id == "j-1"
    assert results[0].error_type == ErrorType.http_error


async def test_query_empty(store):
    results = await store.query_errors(ErrorFilters(project="nonexistent"))
    assert results == []
