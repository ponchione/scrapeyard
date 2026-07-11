# Enforce Per-Target Browser Concurrency

Priority: P0

## Problem

`SCRAPEYARD_WORKERS_MAX_BROWSERS` currently limits jobs that contain at least one browser target, not active browser target executions. A single multi-target job can run many `dynamic` or `stealthy` targets concurrently while consuming one browser permit and reporting one active browser.

Relevant code:

- `src/scrapeyard/queue/pool.py`
- `src/scrapeyard/queue/worker.py`
- `src/scrapeyard/api/dependencies.py`
- `src/scrapeyard/runtime/health.py`

## Required Outcome

The configured browser limit must be a process-wide ceiling on simultaneously executing browser targets. Health data must report actual browser-target activity.

## Implementation Scope

1. Introduce a browser-execution limiter that can be injected into worker target execution.
2. Acquire a permit immediately before a `dynamic` or `stealthy` target starts its browser fetch and release it in `finally`.
3. Do not consume browser permits for `basic` targets.
4. Remove or replace the current job-level `needs_browser` semaphore behavior.
5. Make `active_browsers` count active browser targets rather than browser-bearing jobs.
6. Preserve the Redis/arq queue path and per-job target concurrency.

## Acceptance Criteria

- With `workers_max_browsers=2`, no test can observe more than two simultaneous browser-target executions, including targets from the same job.
- Basic targets remain able to execute while browser permits are exhausted.
- Permits and counters recover after success, failure, cancellation, and shutdown.
- `/health` reports the actual active browser-target count.
- Existing sync, async, scheduled, and live-Redis behavior remains compatible.

## Verification

- Add concurrent unit tests spanning multiple browser targets in one job and multiple jobs.
- Add cancellation and exception-path tests.
- Run `poetry run ruff check src tests`.
- Run `poetry run pytest tests/unit/test_pool.py tests/unit/test_worker_decomposition.py`.
- Run the full test suite and corrected live-Redis lane.

## Non-Goals

- Do not introduce a separate worker service in this task.
- Do not change YAML `execution.concurrency` semantics.
