# Task 01: Add scheduled trigger to enqueue call

**File:** `src/scrapeyard/scheduler/cron.py`
**Action:** modify
**Spec ref:** §9.1

## Change

In `_trigger_job()`, add `trigger="scheduled"` kwarg to the
`self._pool.enqueue()` call (line 130-136). This tags queue entries
originating from the scheduler so downstream consumers can distinguish
scheduled runs from on-demand requests.

## Verify

`grep "trigger=" src/scrapeyard/scheduler/cron.py` shows `trigger="scheduled"`
