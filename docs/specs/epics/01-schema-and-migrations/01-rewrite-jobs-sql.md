# Task 01: Rewrite jobs SQL

**File:** `sql/001_create_jobs.sql`
**Action:** modify
**Spec ref:** §2.2

## Change

Remove the `last_run_at TEXT` and `run_count INTEGER NOT NULL DEFAULT 0` columns
from the `jobs` table definition. The resulting column set must be:

`job_id`, `project`, `name`, `status`, `config_yaml`, `created_at`, `updated_at`,
`schedule_cron`, `schedule_enabled`, `current_run_id`.

Retain the existing `UNIQUE(project, name)` constraint.

## Verify

```bash
sqlite3 :memory: < sql/001_create_jobs.sql
```
