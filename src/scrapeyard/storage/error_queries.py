"""Query helpers for SQLite error storage."""

from __future__ import annotations

from scrapeyard.common.dt import fmt_dt
from scrapeyard.models.job import ErrorFilters


ERROR_SELECT_COLUMNS = (
    "id, job_id, run_id, project, target_url, attempt, "
    "timestamp, error_type, http_status, fetcher_used, "
    "error_message, selectors_matched, action_taken, resolved"
)


def build_query_errors_query(
    filters: ErrorFilters,
    limit: int | None,
    offset: int,
) -> tuple[str, list[object]]:
    clauses: list[str] = []
    params: list[object] = []

    if filters.project is not None:
        clauses.append("project = ?")
        params.append(filters.project)
    if filters.job_id is not None:
        clauses.append("job_id = ?")
        params.append(filters.job_id)
    if filters.since is not None:
        clauses.append("timestamp >= ?")
        params.append(fmt_dt(filters.since))
    if filters.error_type is not None:
        clauses.append("error_type = ?")
        params.append(filters.error_type.value)

    sql = f"SELECT {ERROR_SELECT_COLUMNS} FROM errors"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY timestamp DESC, id DESC"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])
    elif offset > 0:
        sql += " LIMIT -1 OFFSET ?"
        params.append(offset)
    return sql, params
