# Task 01: Update models — add JobRun, trim Job, extend ErrorRecord

**File:** `src/scrapeyard/models/job.py`
**Action:** modify
**Spec ref:** §6.1, §6.2, §6.3

## Change

Three changes in one file:

1. Add `JobRun` model after `Job` class (fields: run_id, job_id, status, trigger, config_hash, started_at, completed_at, record_count, error_count — see spec §6.3).
2. Remove `last_run_at` and `run_count` fields from `Job` model. Retain `current_run_id`.
3. Add `run_id: str` field to `ErrorRecord` model (required, no default).

## Verify

```bash
python -c "from scrapeyard.models.job import JobRun, Job, ErrorRecord"
```
