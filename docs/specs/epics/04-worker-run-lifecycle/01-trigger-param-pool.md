# Task 01: Add trigger param to pool.py

**File:** `src/scrapeyard/queue/pool.py`
**Action:** modify
**Spec ref:** §5.1

## Change

Add `trigger: str = "adhoc"` kwarg to three methods:

1. `enqueue()` — add to signature and pass as positional arg to
   `_redis.enqueue_job()` after `run_id`.
2. `_run_job()` — add after `run_id` positional param, pass to `_execute()`.
3. `_execute()` — add as kwarg, pass to `_task_handler()`.

## Verify

```bash
poetry run ruff check src/scrapeyard/queue/pool.py
```
