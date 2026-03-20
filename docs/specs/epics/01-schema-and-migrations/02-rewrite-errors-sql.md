# Task 02: Rewrite errors SQL

**File:** `sql/002_create_errors.sql`
**Action:** modify
**Spec ref:** §2.3

## Change

Add a `run_id TEXT NOT NULL` column after `job_id` in the `errors` table
definition. Add a new index:

```sql
CREATE INDEX IF NOT EXISTS idx_errors_run_id ON errors (run_id);
```

All existing indexes remain unchanged.

## Verify

```bash
sqlite3 :memory: < sql/002_create_errors.sql
```
