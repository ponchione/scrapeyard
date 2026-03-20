# Task 03: Rewrite results_meta SQL

**File:** `sql/003_create_results_meta.sql`
**Action:** modify
**Spec ref:** §2.4

## Change

Remove the `format TEXT NOT NULL` column from the `results_meta` table
definition. No other changes to the table or its indexes.

## Verify

```bash
sqlite3 :memory: < sql/003_create_results_meta.sql
```
