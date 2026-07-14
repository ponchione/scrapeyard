ALTER TABLE jobs
    ADD COLUMN lifetime_run_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE jobs
    ADD COLUMN last_run_at TEXT;

UPDATE jobs
SET lifetime_run_count = (
        SELECT COUNT(*) FROM job_runs WHERE job_runs.job_id = jobs.job_id
    ),
    last_run_at = (
        SELECT MAX(started_at) FROM job_runs WHERE job_runs.job_id = jobs.job_id
    );

CREATE TRIGGER job_runs_lifetime_summary_after_insert
AFTER INSERT ON job_runs
BEGIN
    UPDATE jobs
    SET lifetime_run_count = lifetime_run_count + 1,
        last_run_at = CASE
            WHEN last_run_at IS NULL OR NEW.started_at > last_run_at
                THEN NEW.started_at
            ELSE last_run_at
        END
    WHERE job_id = NEW.job_id;
END;

CREATE INDEX idx_jobs_adhoc_history_retention
    ON jobs (status, updated_at, job_id)
    WHERE schedule_cron IS NULL;

CREATE INDEX idx_job_runs_scheduled_history_retention
    ON job_runs (job_id, status, completed_at, started_at, run_id);
