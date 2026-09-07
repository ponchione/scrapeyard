-- Current nonterminal owners are the admission reservations. Keep counting
-- them independent of accumulated terminal job history.
CREATE INDEX idx_jobs_admission ON jobs (status, current_run_id)
    WHERE status IN ('queued', 'running') AND current_run_id IS NOT NULL;
