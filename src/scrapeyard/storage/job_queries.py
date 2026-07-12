"""Reporting/query helpers for SQLite job storage."""

from __future__ import annotations

from scrapeyard.storage.job_sql import JOB_COLUMNS, select_columns


def build_list_jobs_with_stats_query(
    project: str | None,
    limit: int | None,
    offset: int,
) -> tuple[str, list[object]]:
    job_cols = select_columns(JOB_COLUMNS, table_alias="j")
    stats_source = "    FROM job_runs r INDEXED BY idx_job_runs_job_started "
    params: list[object] = []
    if project is not None:
        stats_source += (
            "    JOIN jobs filtered_jobs ON filtered_jobs.job_id = r.job_id "
            "    WHERE filtered_jobs.project = ? "
        )
        params.append(project)
    sql = (
        "WITH job_stats AS ("
        "    SELECT r.job_id, COUNT(r.run_id) AS run_count, "
        "           MAX(r.started_at) AS last_run_at "
        + stats_source
        + "    GROUP BY r.job_id"
        ") "
        f"SELECT {job_cols}, "
        "COALESCE(s.run_count, 0) AS run_count, "
        "s.last_run_at "
        "FROM jobs j "
        "LEFT JOIN job_stats s ON j.job_id = s.job_id"
    )
    if project is not None:
        sql += " WHERE j.project = ?"
        params.append(project)
    sql += (
        " ORDER BY COALESCE(s.last_run_at, j.updated_at, j.created_at) DESC, "
        "j.created_at DESC, j.job_id DESC"
    )
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])
    elif offset > 0:
        sql += " LIMIT -1 OFFSET ?"
        params.append(offset)
    return sql, params


PROJECT_SUMMARY_QUERY = (
    "SELECT project, status, COUNT(*) AS count FROM jobs GROUP BY project, status"
)

SCHEDULED_JOBS_QUERY = (
    "SELECT job_id, schedule_cron, schedule_timezone, schedule_enabled "
    "FROM jobs WHERE schedule_cron IS NOT NULL "
    "AND status NOT IN ('cancelled', 'deleting')"
)
