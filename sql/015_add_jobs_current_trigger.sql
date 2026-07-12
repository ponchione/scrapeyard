ALTER TABLE jobs ADD COLUMN current_trigger TEXT;

UPDATE jobs
SET current_trigger = (
    SELECT job_runs.trigger
    FROM job_runs
    WHERE job_runs.job_id = jobs.job_id
      AND job_runs.run_id = jobs.current_run_id
)
WHERE current_run_id IS NOT NULL
  AND status = 'running';
