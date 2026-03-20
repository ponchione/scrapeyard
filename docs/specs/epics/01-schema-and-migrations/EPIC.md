# Epic 1: Schema & Migrations

**Parent spec:** `docs/specs/run-model-and-api-contract.md`
**Spec sections:** 2.1–2.4, 7.4, 12
**Dependencies:** None (foundational — all other epics depend on this)

---

## Goal

Rewrite all four SQL migration files for a fresh-DB deployment and update the
migration runner. No application code changes — this epic is pure schema work.

---

## Tasks

### 1.1 Rewrite `sql/001_create_jobs.sql`

Remove columns `run_count` and `last_run_at` from the `jobs` table. Retain
`current_run_id`. Add `UNIQUE (project, name)` constraint. Full target schema
in spec §2.2.

### 1.2 Rewrite `sql/002_create_errors.sql`

Add `run_id TEXT NOT NULL` column to `errors`. Add index
`idx_errors_run_id`. Full target schema in spec §2.3.

### 1.3 Rewrite `sql/003_create_results_meta.sql`

Remove `format` column from `results_meta`. Full target schema in spec §2.4.

### 1.4 Create `sql/004_create_job_runs.sql`

New table `job_runs` with columns: `run_id`, `job_id`, `status`, `trigger`,
`config_hash`, `started_at`, `completed_at`, `record_count`, `error_count`.
Indexes on `job_id` and `started_at`. Full schema in spec §2.1.

### 1.5 Update `src/scrapeyard/storage/database.py`

Add `004_create_job_runs.sql` to the migration file list so the startup
migration runner picks it up.

---

## Acceptance Criteria

- All four `.sql` files match the spec schemas exactly.
- `database.py` migration runner discovers and executes all four files.
- Application starts cleanly against an empty DB directory.
- Existing `*.db` files are deleted before first run (manual step, noted in spec §12).

---

## Files Touched

| File | Action |
|---|---|
| `sql/001_create_jobs.sql` | Rewrite |
| `sql/002_create_errors.sql` | Rewrite |
| `sql/003_create_results_meta.sql` | Rewrite |
| `sql/004_create_job_runs.sql` | Create |
| `src/scrapeyard/storage/database.py` | Modify (migration list) |
