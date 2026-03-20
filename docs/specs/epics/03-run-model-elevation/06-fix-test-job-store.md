# Task 06: Fix test_job_store for removed fields

**File:** `tests/unit/test_job_store.py`
**Action:** modify
**Spec ref:** n/a (test alignment)

## Change

Remove `assert fetched.run_count == 0` (line 42). Remove `"run_count": 1` from model_copy update dict (line 89). Remove `assert fetched.run_count == 1` (line 97).

## Verify

```bash
poetry run pytest tests/unit/test_job_store.py
```
