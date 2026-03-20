CREATE TABLE IF NOT EXISTS job_runs (
    run_id        TEXT PRIMARY KEY,
    job_id        TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'running',
    trigger       TEXT NOT NULL,
    config_hash   TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    completed_at  TEXT,
    record_count  INTEGER,
    error_count   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_job_runs_job_id ON job_runs (job_id);
CREATE INDEX IF NOT EXISTS idx_job_runs_started_at ON job_runs (started_at);
