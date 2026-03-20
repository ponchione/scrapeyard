# Task 02: Update jobs list response

**File:** `src/scrapeyard/api/routes.py`
**Action:** modify
**Spec ref:** §4.2, §8.2

## Change

In `GET /jobs` handler: replace `job_store.list_jobs(project)` with
`job_store.list_jobs_with_stats(project)`. Build response dicts with derived
`run_count` and `last_run_at`. Delete the now-unused `_job_to_dict` helper
function (references removed fields).

## Verify

List response includes derived `run_count`/`last_run_at`.
