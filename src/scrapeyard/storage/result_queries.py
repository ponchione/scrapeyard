"""Query helpers for SQLite result storage."""

from __future__ import annotations


def build_result_lookup_query(
    job_id: str,
    run_id: str | None,
) -> tuple[str, tuple[object, ...]]:
    if run_id is not None:
        return (
            "SELECT run_id, file_path FROM results_meta"
            " WHERE job_id=? AND run_id=?",
            (job_id, run_id),
        )
    return (
        "SELECT run_id, file_path FROM results_meta"
        " WHERE job_id=? ORDER BY created_at DESC LIMIT 1",
        (job_id,),
    )


EXPIRED_RESULTS_QUERY = (
    "SELECT id, file_path FROM results_meta WHERE created_at < ?"
)

JOB_RESULTS_DELETE_QUERY = (
    "SELECT id, file_path FROM results_meta WHERE job_id=?"
)

EXCESS_RESULTS_PER_JOB_QUERY = """
SELECT id, file_path FROM (
    SELECT id, file_path,
           ROW_NUMBER() OVER (
               PARTITION BY job_id
               ORDER BY created_at DESC
           ) AS rn
    FROM results_meta
) WHERE rn > ?
"""
