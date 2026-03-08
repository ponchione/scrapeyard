# Result Retention Auto-Cleanup Design

**Work order:** 018-implement-result-retention-auto-cleanup
**Date:** 2026-03-08
**Approach:** Add `run_cleanup` coroutine alongside existing `delete_expired`

## Problem

The cleanup loop (from WO-016) handles age-based retention but lacks `max_results_per_job` pruning. WO-018 requires a standalone `run_cleanup` coroutine that operates directly on the DB and filesystem for both cleanup dimensions.

## Design

### `run_cleanup(results_dir, retention_days, max_results_per_job, db)`

Standalone async coroutine in `cleanup.py` that:
1. Deletes results older than `retention_days` (query `results_meta` for `created_at < cutoff`, delete files + rows)
2. Groups remaining results by `job_id`, for any job exceeding `max_results_per_job`, deletes the oldest runs keeping the most recent N
3. Operates directly on `aiosqlite.Connection` and filesystem — does not use `ResultStore`

### `start_cleanup_loop(interval_hours=6)`

Updated signature. Internally loads settings via `get_settings()`, gets DB via `get_db()`, calls `run_cleanup`. No longer takes `result_store` parameter.

### `main.py` lifespan

Simplified call — `start_cleanup_loop()` with no arguments (uses defaults + settings).

### Existing code

`delete_expired` on `LocalResultStore` and `ResultStore` protocol stays unchanged (added by WO-016).

## Constraints

- Do NOT modify `protocols.py`
- Do NOT modify `ResultStore` interface
- Cleanup operates directly on DB and filesystem
- No new external dependencies
