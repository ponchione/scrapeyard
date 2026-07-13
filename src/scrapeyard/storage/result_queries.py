"""Query helpers for SQLite result storage."""

from __future__ import annotations


def build_result_lookup_query(
    job_id: str,
    run_id: str | None,
) -> tuple[str, tuple[object, ...]]:
    if run_id is not None:
        return (
            "SELECT job_id, project, run_id, status, record_count, file_path, created_at"
            " FROM results_meta"
            " WHERE job_id=? AND run_id=?",
            (job_id, run_id),
        )
    return (
        "SELECT job_id, project, run_id, status, record_count, file_path, created_at"
        " FROM results_meta"
        " WHERE job_id=? ORDER BY created_at DESC, id DESC LIMIT 1",
        (job_id,),
    )


EXPIRED_RESULTS_QUERY = (
    "SELECT id, file_path FROM results_meta WHERE created_at < ? "
    "ORDER BY created_at ASC, id ASC LIMIT ?"
)

JOB_RESULTS_DELETE_QUERY = "SELECT id, file_path FROM results_meta WHERE job_id=?"

EXCESS_RESULTS_PER_JOB_QUERY = """
SELECT id, file_path FROM (
    SELECT id, file_path, created_at,
           ROW_NUMBER() OVER (
               PARTITION BY job_id
               ORDER BY created_at DESC, id DESC
           ) AS rn
    FROM results_meta
) WHERE rn > ?
ORDER BY created_at ASC, id ASC
LIMIT ?
"""
