-- A2: Composite indexes for results_meta.db.

-- get_result does WHERE job_id=? ORDER BY created_at DESC LIMIT 1.
CREATE INDEX IF NOT EXISTS idx_results_meta_job_created
    ON results_meta (job_id, created_at DESC);

-- save_result DELETE path uses WHERE job_id=? AND run_id=?.
CREATE INDEX IF NOT EXISTS idx_results_meta_job_run
    ON results_meta (job_id, run_id);
