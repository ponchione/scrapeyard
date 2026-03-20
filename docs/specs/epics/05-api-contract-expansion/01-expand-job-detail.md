# Task 01: Expand job detail response

**File:** `src/scrapeyard/api/routes.py`
**Action:** modify
**Spec ref:** §4.1, §8.1

## Change

In `GET /jobs/{job_id}` handler: add `SchedulerService` dependency via `get_scheduler`.
Fetch `job_store.get_job_runs(job_id, limit=10)`, `job_store.get_job_run_stats(job_id)`,
and `scheduler.get_next_run_time(job_id)`. Build expanded response with `config_yaml`,
`next_run_at`, `run_count`, `last_run_at`, and `runs` array (last 10, newest-first).

## Verify

Response shape matches spec §4.1.
