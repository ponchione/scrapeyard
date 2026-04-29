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
)

JOB_RUN_COLUMNS = (
    "run_id",
    "job_id",
    "status",
    "trigger",
    "config_hash",
    "started_at",
    "completed_at",
    "record_count",
    "error_count",
)


def select_columns(columns: tuple[str, ...], *, table_alias: str | None = None) -> str:
    prefix = "" if table_alias is None else f"{table_alias}."
    return ", ".join(f"{prefix}{column}" for column in columns)
