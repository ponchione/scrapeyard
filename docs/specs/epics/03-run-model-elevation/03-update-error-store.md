# Task 03: Update error store for run_id

**File:** `src/scrapeyard/storage/error_store.py`
**Action:** modify
**Spec ref:** §7.2

## Change

Three changes:

1. `log_error()` INSERT — add `run_id` column after job_id, add `error.run_id` to values tuple.
2. `_row_to_error()` — add `run_id=row[2]`, shift all subsequent indices +1 (project becomes row[3], etc.).
3. `query_errors()` SELECT — add `run_id` after `job_id` in column list.

## Verify

```bash
ruff check src/scrapeyard/storage/error_store.py
```
