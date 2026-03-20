# Task 02: Expose next run time for a scheduled job

**File:** `src/scrapeyard/scheduler/cron.py`
**Action:** modify
**Spec ref:** §9.2

## Change

Add `get_next_run_time(self, job_id: str) -> datetime | None` method to
`SchedulerService`. Implementation: get the APScheduler job via
`self._scheduler.get_job(job_id)`, then return
`aps_job.next_run_time if aps_job else None`.

## Verify

`ruff check src/scrapeyard/scheduler/cron.py`
