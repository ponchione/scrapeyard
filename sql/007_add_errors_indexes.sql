-- A2: Composite indexes for errors.db.

-- query_errors filters by (job_id, timestamp) and (project, timestamp).
CREATE INDEX IF NOT EXISTS idx_errors_job_timestamp
    ON errors (job_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_errors_project_timestamp
    ON errors (project, timestamp);
