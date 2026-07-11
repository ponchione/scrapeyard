ALTER TABLE job_runs ADD COLUMN webhook_reconciled_at TEXT;

CREATE INDEX idx_job_runs_terminal_reconciliation
    ON job_runs (status, webhook_reconciled_at)
    WHERE status IN ('complete', 'partial', 'failed');
