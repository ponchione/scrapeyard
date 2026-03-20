CREATE TABLE IF NOT EXISTS errors (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id           TEXT NOT NULL,
    run_id           TEXT NOT NULL,
    project          TEXT NOT NULL,
    target_url       TEXT NOT NULL,
    attempt          INTEGER NOT NULL,
    timestamp        TEXT NOT NULL,
    error_type       TEXT NOT NULL,
    http_status      INTEGER,
    fetcher_used     TEXT NOT NULL,
    error_message    TEXT,
    selectors_matched TEXT,
    action_taken     TEXT NOT NULL,
    resolved         INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_errors_project   ON errors (project);
CREATE INDEX IF NOT EXISTS idx_errors_job_id    ON errors (job_id);
CREATE INDEX IF NOT EXISTS idx_errors_run_id    ON errors (run_id);
CREATE INDEX IF NOT EXISTS idx_errors_timestamp ON errors (timestamp);
