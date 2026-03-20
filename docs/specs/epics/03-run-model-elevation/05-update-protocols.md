# Task 05: Update storage protocols

**File:** `src/scrapeyard/storage/protocols.py`
**Action:** modify
**Spec ref:** §7.1, §5.4

## Change

Three changes:

1. `ResultStore.save_result` — remove `format` and `file_contents` params.
2. `ResultStore.get_result` — return `ResultPayload` instead of `Any`.
3. `JobStore` — add `get_job_runs()`, `get_job_run_stats()`, `list_jobs_with_stats()` method signatures. Add needed imports (JobRun, ResultPayload, datetime).

## Verify

```bash
ruff check src/scrapeyard/storage/protocols.py
```
