# Task 05: Update migration runner

**File:** `src/scrapeyard/storage/database.py`
**Action:** modify
**Spec ref:** §7.4, §12

## Change

Three changes to the migration runner:

1. Change the `_DB_MIGRATIONS` value type from `str` to `list[str]` so that
   `"jobs.db"` maps to `["001_create_jobs.sql", "004_create_job_runs.sql"]`
   and the other databases become single-element lists.
2. Update `init_db()` to iterate the list of migration files per database,
   executing each in order.
3. Remove the `_ensure_job_columns()` and `_ensure_error_columns()` backfill
   helpers entirely (fresh-database-only going forward).

## Verify

```bash
ruff check src/scrapeyard/storage/database.py
```

App starts cleanly with an empty DB dir.
