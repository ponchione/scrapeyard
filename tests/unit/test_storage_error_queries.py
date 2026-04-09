from __future__ import annotations

from datetime import datetime

from scrapeyard.models.job import ErrorFilters, ErrorType
from scrapeyard.storage.error_queries import build_query_errors_query


def test_build_query_errors_query_with_all_filters_and_pagination() -> None:
    sql, params = build_query_errors_query(
        ErrorFilters(
            project="acme",
            job_id="job-1",
            since=datetime(2026, 3, 1, 12, 0, 0),
            error_type=ErrorType.timeout,
        ),
        limit=20,
        offset=5,
    )

    assert "FROM errors" in sql
    assert "project = ?" in sql
    assert "job_id = ?" in sql
    assert "timestamp >= ?" in sql
    assert "error_type = ?" in sql
    assert "ORDER BY timestamp DESC, id DESC" in sql
    assert "LIMIT ? OFFSET ?" in sql
    assert params == [
        "acme",
        "job-1",
        "2026-03-01T12:00:00",
        "timeout",
        20,
        5,
    ]


def test_build_query_errors_query_uses_offset_without_limit() -> None:
    sql, params = build_query_errors_query(ErrorFilters(), limit=None, offset=2)

    assert "LIMIT -1 OFFSET ?" in sql
    assert params == [2]
