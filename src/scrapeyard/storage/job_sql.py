"""Shared SQL column constants for SQLite job storage."""

JOB_COLUMNS = (
    "job_id, project, name, status, config_yaml, "
    "created_at, updated_at, schedule_cron, schedule_enabled, "
    "current_run_id"
)

JOB_RUN_COLUMNS = (
    "run_id, job_id, status, trigger, config_hash, "
    "started_at, completed_at, record_count, error_count"
)
