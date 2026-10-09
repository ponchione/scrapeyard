-- idx_results_meta_job_run, idx_results_meta_job_created and
-- idx_results_meta_project_run serve every lookup these single-column indexes
-- did.
DROP INDEX IF EXISTS idx_results_meta_job_id;
DROP INDEX IF EXISTS idx_results_meta_project;
