-- UNIQUE (project, name) in 001_create_jobs.sql already maintains
-- an autoindex with project as its leading column, so every project lookup
-- uses that index and each job write maintains one fewer.
DROP INDEX IF EXISTS idx_jobs_project;
