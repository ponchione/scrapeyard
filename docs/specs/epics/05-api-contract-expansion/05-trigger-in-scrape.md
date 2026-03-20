# Task 05: Add trigger to scrape enqueue

**File:** `src/scrapeyard/api/routes.py`
**Action:** modify
**Spec ref:** §8.5

## Change

In `POST /scrape` handler: add `trigger="adhoc"` kwarg to
`worker_pool.enqueue()` call.

## Verify

`grep "trigger=" src/scrapeyard/api/routes.py` shows `trigger="adhoc"`.
