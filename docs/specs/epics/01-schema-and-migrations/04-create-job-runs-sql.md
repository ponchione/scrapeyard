# Task 04: Create job_runs SQL

**File:** `sql/004_create_job_runs.sql`
**Action:** create
**Spec ref:** §2.1

## Change

Create a new migration file defining the `job_runs` table with columns:
`run_id` (TEXT PK), `job_id` (TEXT NOT NULL), `status` (TEXT NOT NULL),
`trigger` (TEXT NOT NULL), `config_hash` (TEXT NOT NULL), `started_at` (TEXT),
`completed_at` (TEXT), `record_count` (INTEGER NOT NULL DEFAULT 0),
`error_count` (INTEGER NOT NULL DEFAULT 0).

Add indexes on `job_id` and `started_at`. Full DDL is specified in §2.1.

## Verify

```bash
sqlite3 :memory: < sql/004_create_job_runs.sql
```
