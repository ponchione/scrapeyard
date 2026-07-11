# SQL Migrations

This directory contains SQL migration scripts for the Scrapeyard database schema (SQLite).

## Naming Convention

Files follow the pattern `NNN_description.sql` where `NNN` is a zero-padded
three-digit global sequence number. Each file is explicitly assigned to one
database in `scrapeyard.storage.database._DB_MIGRATIONS`.

Applied migrations are recorded in each database's `schema_migrations` table
with their filename, SHA-256 checksum, and application timestamp. Startup
applies only missing migrations and fails on gaps, assignment drift, or checksum
drift. Existing pre-ledger databases are baselined only after the expected schema
objects are verified.

## Adding a New Migration

1. Use the next global sequence number without gaps.
2. Create the forward-only SQL file and assign it to exactly one database in
   `_DB_MIGRATIONS`.
3. Never edit an applied migration. Its checksum is part of persisted history.
4. Prefer migration SQL that fails atomically rather than attempting to repair
   ambiguous state. The migration runner wraps the SQL and ledger insert in one
   SQLite transaction.
5. Add fresh-install, upgrade, repeat-startup, and rollback tests.

## Existing Migrations

| File | Purpose |
|------|---------|
| `001_create_jobs.sql` | `jobs` table — tracks scrape jobs and their scheduling/status. |
| `002_create_errors.sql` | `errors` table — structured error records with indexes on project, job_id, and timestamp. |
| `003_create_results_meta.sql` | `results_meta` table — metadata for scrape result files with indexes on job_id and project. |
| `004_create_job_runs.sql` | Run history and ownership state. |
| `005_add_indexes.sql` | Composite job and run indexes. |
| `006_add_results_meta_indexes.sql` | Composite result metadata indexes. |
| `007_add_errors_indexes.sql` | Composite error-query indexes. |
| `008_results_meta_unique_job_run.sql` | Deduplicate and enforce one metadata row per job/run. |
| `009_create_webhook_outbox.sql` | Durable webhook outbox and operational indexes. |
