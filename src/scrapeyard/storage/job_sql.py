"""Shared SQL column constants for SQLite job storage."""

JOB_COLUMNS = (
    "job_id",
    "project",
    "name",
    "status",
    "config_yaml",
    "created_at",
    "updated_at",
    "schedule_cron",
    "schedule_enabled",
    "current_run_id",
    "deletion_requested_at",
    "delete_results_on_delete",
)

JOB_RUN_COLUMNS = (
    "run_id",
    "job_id",
    "status",
    "trigger",
    "config_hash",
    "started_at",
    "heartbeat_at",
    "completed_at",
    "record_count",
    "error_count",
)


def select_columns(columns: tuple[str, ...], *, table_alias: str | None = None) -> str:
    prefix = "" if table_alias is None else f"{table_alias}."
    return ", ".join(f"{prefix}{column}" for column in columns)
