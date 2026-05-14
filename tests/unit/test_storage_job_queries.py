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


def test_build_list_jobs_with_stats_query_filters_empty_project() -> None:
    sql, params = build_list_jobs_with_stats_query(project="", limit=5, offset=0)

    assert "WHERE j.project = ?" in sql
    assert params == ["", 5, 0]


def test_build_list_jobs_with_stats_query_supports_offset_without_limit() -> None:
    sql, params = build_list_jobs_with_stats_query(project=None, limit=None, offset=3)

    assert "LIMIT -1 OFFSET ?" in sql
    assert params == [3]


def test_row_to_job_with_stats_maps_run_count_and_timestamp() -> None:
    job, run_count, last_run_at = row_to_job_with_stats(
        {
            "job_id": "j-1",
            "project": "acme",
            "name": "prices",
            "status": "queued",
            "config_yaml": "target: https://example.com",
            "created_at": "2026-03-01T08:00:00",
            "updated_at": "2026-03-02T09:00:00",
            "schedule_cron": "*/5 * * * *",
            "schedule_enabled": 1,
            "current_run_id": "run-123",
            "run_count": 7,
            "last_run_at": "2026-03-03T10:00:00",
        }
    )

    assert job.job_id == "j-1"
    assert run_count == 7
    assert last_run_at == datetime(2026, 3, 3, 10, 0, 0)


def test_row_to_project_summary_maps_count_tuple() -> None:
    assert row_to_project_summary({"project": "acme", "status": "running", "count": 4}) == (
        "acme",
        "running",
        4,
    )


def test_row_to_schedule_state_coerces_boolean_flag() -> None:
    assert row_to_schedule_state({
        "job_id": "job-1",
        "schedule_cron": "0 * * * *",
        "schedule_enabled": 0,
    }) == (
        "job-1",
        "0 * * * *",
        False,
    )
