CREATE TABLE queued_run_snapshots (
    run_id      TEXT PRIMARY KEY,
    job_id      TEXT NOT NULL,
    trigger     TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    config_yaml TEXT NOT NULL,
    queued_at   TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);

CREATE INDEX idx_queued_run_snapshots_job_id
    ON queued_run_snapshots (job_id, run_id);
