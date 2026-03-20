# Task 03: Update results endpoint

**File:** `src/scrapeyard/api/routes.py`
**Action:** modify
**Spec ref:** §4.3, §8.3

## Change

In `GET /results/{job_id}` handler: `result_store.get_result()` now returns
`ResultPayload`. Use `.run_id` and `.data` — include `run_id` in response
envelope. Also update `POST /scrape` sync path to handle `ResultPayload`
return (use `.data` for result).

## Verify

Results response includes `run_id` field.
