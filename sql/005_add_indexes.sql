-- A2: Composite indexes for jobs.db tables.

-- jobs: list_jobs and list_jobs_with_stats filter by project.
CREATE INDEX IF NOT EXISTS idx_jobs_project ON jobs (project);

-- job_runs: get_job_runs does WHERE job_id=? ORDER BY started_at DESC LIMIT ?.
CREATE INDEX IF NOT EXISTS idx_job_runs_job_started
    ON job_runs (job_id, started_at DESC);
