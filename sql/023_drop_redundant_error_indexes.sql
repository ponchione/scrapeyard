-- idx_errors_project_timestamp and idx_errors_job_timestamp serve every lookup
-- these single-column indexes did, so each error insert maintains two fewer.
DROP INDEX IF EXISTS idx_errors_project;
DROP INDEX IF EXISTS idx_errors_job_id;
