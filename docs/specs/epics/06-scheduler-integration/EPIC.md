# Epic 6: Scheduler Integration

**Parent spec:** `docs/specs/run-model-and-api-contract.md`
**Spec sections:** 9.1–9.2
**Dependencies:** Epic 4 (trigger param must exist on enqueue)

---

## Goal

Wire the scheduler to pass `trigger="scheduled"` when it fires jobs, and
expose `get_next_run_time()` for the expanded job detail endpoint.

---

## Tasks

### 6.1 Pass `trigger="scheduled"` in `_trigger_job`

In `SchedulerService._trigger_job`, pass `trigger="scheduled"` to
`self._pool.enqueue()`.

### 6.2 Add `get_next_run_time()` method

```python
def get_next_run_time(self, job_id: str) -> datetime | None:
    """Return the next scheduled fire time, or None if not scheduled."""
    aps_job = self._scheduler.get_job(job_id)
    return aps_job.next_run_time if aps_job else None
```

Returns `None` for non-scheduled jobs or if the scheduler hasn't registered
the job yet.

---

## Acceptance Criteria

- Scheduled runs produce `job_runs` rows with `trigger = 'scheduled'`.
- Ad-hoc runs produce `job_runs` rows with `trigger = 'adhoc'`.
- `get_next_run_time()` returns the correct next fire time for scheduled jobs.
- `get_next_run_time()` returns `None` for jobs without schedules.

---

## Files Touched

| File | Action |
|---|---|
| `src/scrapeyard/scheduler/cron.py` | Modify (trigger param, new method) |
