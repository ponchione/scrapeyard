# Task 04: Update errors endpoint

**File:** `src/scrapeyard/api/routes.py`
**Action:** modify
**Spec ref:** §4.4, §8.4

## Change

In `GET /errors` handler: add `"run_id": e.run_id` to each error response
dict, after `job_id`.

## Verify

Each error object includes `run_id`.
