ALTER TABLE job_runs
    ADD COLUMN config_yaml TEXT;

ALTER TABLE job_runs
    ADD COLUMN failure_code TEXT;

ALTER TABLE job_runs
    ADD COLUMN webhook_reconciliation_failed_at TEXT;

ALTER TABLE jobs
    ADD COLUMN schedule_failure_at TEXT;

ALTER TABLE jobs
    ADD COLUMN schedule_failure_code TEXT;

ALTER TABLE jobs
    ADD COLUMN schedule_consecutive_failures INTEGER NOT NULL DEFAULT 0;

CREATE INDEX idx_jobs_schedule_failures
    ON jobs (schedule_failure_at, job_id)
    WHERE schedule_failure_at IS NOT NULL;

CREATE INDEX idx_job_runs_terminal_reconciliation_retry
    ON job_runs (webhook_reconciliation_failed_at, completed_at, job_id, run_id)
    WHERE webhook_reconciled_at IS NULL
      AND status IN ('complete', 'partial', 'failed');
