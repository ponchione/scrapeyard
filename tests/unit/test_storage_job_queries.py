from __future__ import annotations

from datetime import datetime

from scrapeyard.storage.job_queries import build_list_jobs_with_stats_query
from scrapeyard.storage.job_rows import (
    row_to_job_with_stats,
    row_to_project_summary,
    row_to_schedule_state,
)


def test_build_list_jobs_with_stats_query_filters_and_paginates() -> None:
    sql, params = build_list_jobs_with_stats_query(
        project="acme",
        limit=5,
        offset=10,
    )

    assert "WITH job_stats AS" in sql
    assert "WHERE j.project = ?" in sql
    assert "LIMIT ? OFFSET ?" in sql
    assert params == ["acme", 5, 10]


def test_build_list_jobs_with_stats_query_supports_offset_without_limit() -> None:
    sql, params = build_list_jobs_with_stats_query(project=None, limit=None, offset=3)

    assert "LIMIT -1 OFFSET ?" in sql
    assert params == [3]


def test_row_to_job_with_stats_maps_run_count_and_timestamp() -> None:
    job, run_count, last_run_at = row_to_job_with_stats(
        (
            "j-1",
            "acme",
            "prices",
            "queued",
            "target: https://example.com",
            "2026-03-01T08:00:00",
            "2026-03-02T09:00:00",
            "*/5 * * * *",
            1,
            "run-123",
            7,
            "2026-03-03T10:00:00",
        )
    )

    assert job.job_id == "j-1"
    assert run_count == 7
    assert last_run_at == datetime(2026, 3, 3, 10, 0, 0)


def test_row_to_project_summary_maps_count_tuple() -> None:
    assert row_to_project_summary(("acme", "running", 4)) == ("acme", "running", 4)


def test_row_to_schedule_state_coerces_boolean_flag() -> None:
    assert row_to_schedule_state(("job-1", "0 * * * *", 0)) == (
        "job-1",
        "0 * * * *",
        False,
    )
