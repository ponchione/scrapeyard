-- D1: Upgrade (job_id, run_id) index to UNIQUE for atomic INSERT OR REPLACE.
-- Replaces the non-unique composite index from 006.

DROP INDEX IF EXISTS idx_results_meta_job_run;

CREATE UNIQUE INDEX IF NOT EXISTS idx_results_meta_job_run
    ON results_meta (job_id, run_id);
