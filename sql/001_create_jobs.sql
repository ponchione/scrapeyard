CREATE TABLE IF NOT EXISTS jobs (
    job_id           TEXT PRIMARY KEY,
    project          TEXT NOT NULL,
    name             TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'queued',
    config_yaml      TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT,
    schedule_cron    TEXT,
    schedule_enabled INTEGER NOT NULL DEFAULT 1,
    current_run_id   TEXT,
    UNIQUE (project, name)
);
