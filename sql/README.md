# SQL Migrations

This directory contains SQL migration scripts for the Scrapeyard database schema (SQLite).

## Naming Convention

Files follow the pattern `NNN_description.sql` where `NNN` is a zero-padded
three-digit sequence number. Scripts are applied in order during service
initialization.

## Adding a New Migration

1. Determine the next sequence number (e.g., if `003_…` exists, use `004`).
2. Create the file: `sql/004_your_description.sql`.
3. Use `IF NOT EXISTS` guards on all `CREATE TABLE` and `CREATE INDEX`
   statements so that scripts are idempotent.
4. Test locally: `sqlite3 :memory: ".read sql/004_your_description.sql" ".read sql/004_your_description.sql"` —
   running twice must succeed without errors.

## Existing Migrations

| File | Purpose |
|------|---------|
| `001_create_jobs.sql` | `jobs` table — tracks scrape jobs and their scheduling/status. |
| `002_create_errors.sql` | `errors` table — structured error records with indexes on project, job_id, and timestamp. |
| `003_create_results_meta.sql` | `results_meta` table — metadata for scrape result files with indexes on job_id and project. |
