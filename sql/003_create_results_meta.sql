CREATE TABLE IF NOT EXISTS results_meta (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id       TEXT NOT NULL,
    project      TEXT NOT NULL,
    run_id       TEXT NOT NULL,
    status       TEXT NOT NULL,
    record_count INTEGER,
    file_path    TEXT NOT NULL,
    format       TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_results_meta_job_id     ON results_meta (job_id);
CREATE INDEX IF NOT EXISTS idx_results_meta_project    ON results_meta (project);
CREATE INDEX IF NOT EXISTS idx_results_meta_created_at ON results_meta (created_at);
