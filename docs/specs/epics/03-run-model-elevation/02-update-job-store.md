# Task 02: Update job store for run model

**File:** `src/scrapeyard/storage/job_store.py`
**Action:** modify
**Spec ref:** §7.1

## Change

Five changes:

1. Update `_row_to_job()` — remove last_run_at/run_count, shift current_run_id from row[11] to row[9].
2. Update `save_job()` INSERT — remove last_run_at/run_count from columns and values.
3. Update `update_job()` SET — remove last_run_at/run_count.
4. Update `get_job()` and `list_jobs()` SELECTs — remove last_run_at/run_count from column lists.
5. Add three new methods: `get_job_runs(job_id, limit=10)`, `get_job_run_stats(job_id)` returning `(count, last_run_at)`, `list_jobs_with_stats(project)` using LEFT JOIN on job_runs. Add `_row_to_job_run()` helper. Import `JobRun`.

## Verify

```bash
poetry run pytest tests/unit/test_job_store.py
```

(passes after test updates in wave 3)
