# Task 07: Fix test_worker_error_handling for removed fields

**File:** `tests/unit/test_worker_error_handling.py`
**Action:** modify
**Spec ref:** n/a (test alignment)

## Change

Remove `"last_run_at": datetime.now(timezone.utc)` from model_copy update dict in `test_scrape_task_skips_completed_duplicate_run` (line 71).

## Verify

```bash
poetry run pytest tests/unit/test_worker_error_handling.py
```
