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
| `010_add_terminal_reconciliation_marker.sql` | Bound startup terminal-intent reconciliation to unresolved runs. |
| `011_add_results_artifact_lookup_index.sql` | Narrow destructive artifact ownership rechecks by project and run. |
| `012_create_scrape_idempotency.sql` | Caller-scoped, expiring ad-hoc submission deduplication records. |
| `013_add_schedule_timezone.sql` | Persist explicit IANA timezone semantics for scheduled jobs. |
| `014_add_jobs_config_hash.sql` | Preserve plaintext-free config compare-and-set semantics. |
| `015_add_jobs_current_trigger.sql` | Preserve accepted ad-hoc, scheduled, or manual trigger provenance through queued-delivery recovery. |
| `016_add_webhook_decode_failure_reason.sql` | Add a terminal reason for quarantined malformed webhook outbox rows. |
| `017_add_history_retention_summary.sql` | Add lifetime run summaries and bounded history-retention indexes. |
| `018_add_run_snapshots_and_schedule_health.sql` | Add encrypted run configuration snapshots, failure classification, terminal-reconciliation retry state, and durable schedule health. |
| `019_create_queued_run_snapshots.sql` | Persist an encrypted immutable configuration snapshot for each accepted delivery before it enters Redis. |
| `020_create_result_reconciliation_state.sql` | Persist metadata and filesystem keyset cursors so artifact scans remain fair across process restarts. |
