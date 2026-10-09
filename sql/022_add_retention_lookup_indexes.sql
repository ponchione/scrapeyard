-- Retention eligibility, job deletion, submission rollback and the foreign-key
-- cascade find a job's idempotency records by job_id. Without this index each
-- lookup scans every unexpired record, or the whole table.
CREATE INDEX idx_scrape_idempotency_job
    ON scrape_idempotency (job_id, expires_at);

-- Scheduled-history retention starts from the few scheduled jobs instead of
-- reading every job row.
CREATE INDEX idx_jobs_scheduled ON jobs (job_id)
    WHERE schedule_cron IS NOT NULL;

-- idx_job_runs_job_started serves every job_id lookup, and no query filters or
-- sorts runs by started_at alone.
DROP INDEX IF EXISTS idx_job_runs_job_id;
DROP INDEX IF EXISTS idx_job_runs_started_at;
