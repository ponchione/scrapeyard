-- Stale-running recovery reaches owned running jobs through idx_jobs_admission
-- and running jobs without a current run through this index, which stays
-- empty unless that invalid state exists, instead of scanning every job.
CREATE INDEX idx_jobs_running_unowned ON jobs (job_id)
    WHERE status = 'running' AND current_run_id IS NULL;
