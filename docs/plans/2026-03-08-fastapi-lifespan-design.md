# FastAPI Lifespan Wiring Design

**Work order:** 016-wire-up-fastapi-lifespan-for-startup-and-shutdown
**Date:** 2026-03-08
**Approach:** Enhance existing lifespan in-place (Approach A)

## Problem

The current lifespan in `main.py` is incomplete. It starts the scheduler and worker pool but skips database initialization, storage instance creation, and has no result retention cleanup loop. Shutdown is also incomplete — no DB teardown.

## Design

### Startup Order (dependency-driven)

1. Record start time
2. Load settings via `get_settings()`
3. Configure logging via `setup_logging(settings.log_dir)`
4. Initialize database via `init_db(settings.db_dir)` — must precede storage
5. Create storage instances (job_store, result_store, error_store), store on `app.state`
6. Create and start worker pool, store on `app.state`
7. Create and start scheduler (re-loads persisted cron jobs), store on `app.state`
8. Start cleanup loop via `start_cleanup_loop()`, store task on `app.state`

### Shutdown Order (reverse)

1. Cancel cleanup loop task
2. Shut down scheduler
3. Drain worker pool gracefully via `await pool.stop()`
4. Reset DB module state via `reset_db()`

### New File: `src/scrapeyard/storage/cleanup.py`

- `start_cleanup_loop(result_store, retention_days) -> asyncio.Task`
- Spawns an async task that runs an infinite loop
- Every ~1 hour, calls `result_store.delete_expired(retention_days)`
- Returns task handle for cancellation on shutdown
- Handles `asyncio.CancelledError` cleanly

### New Method: `LocalResultStore.delete_expired(retention_days)`

- Queries `results_meta.db` for records older than `retention_days`
- Deletes associated result files from disk
- Deletes the DB rows
- Added to `ResultStore` protocol as well

### New Function: `database.reset_db()`

- Sets module-level `_db_dir = None`
- Clean teardown signal, no persistent connections to close

### `app.state` Usage

Storage instances are stored on `app.state` during startup. The `dependencies.py` lru_cache singletons continue to work — same instances, created once.

### Testing

Existing `test_health.py` must continue to pass. The `init_db` call in lifespan will need test configuration to use a temp directory (via env var or mock).

## Constraints

- Do NOT change Docker CMD or exposed port
- Do NOT modify storage protocol definitions (beyond adding `delete_expired`)
- `app` remains importable as `scrapeyard.main:app`
