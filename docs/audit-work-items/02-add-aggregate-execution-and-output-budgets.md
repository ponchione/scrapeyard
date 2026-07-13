# Add Aggregate Execution and Output Budgets

> **Status: completed for 0.6.0.** This file is a historical design record;
> its Problem section describes the pre-implementation state. See the
> [archive index](README.md) for completion evidence.

Priority: P0

## Problem

Individual config fields are bounded, but their combination can still create an extremely large job. Target count, pagination, retries, browser actions, extracted records, response content, and field sizes have no shared budget. Results are accumulated in memory and serialized as one JSON document. The current memory check only evaluates RSS before enqueue and neither reserves capacity nor stops a running job.

Relevant code:

- `src/scrapeyard/config/schema.py`
- `src/scrapeyard/engine/scraper.py`
- `src/scrapeyard/queue/worker.py`
- `src/scrapeyard/queue/pool.py`
- `src/scrapeyard/storage/result_store.py`
- `src/scrapeyard/common/settings.py`

## Required Outcome

Every run must operate within explicit service-level ceilings and fail in a controlled, observable way when a ceiling is reached.

## Implementation Scope

1. Add `SCRAPEYARD_*` settings for maximum run duration, fetched bytes where supported, extracted records, serialized result bytes, and debug artifact bytes.
2. Enforce an overall run deadline across targets, pagination, retries, rate-limit waits, and browser actions.
3. Stop adding records once the configured record ceiling is reached and choose a documented terminal status/error type.
4. Measure serialized result size before committing metadata; never leave metadata pointing to a rejected or partial file.
5. Bound browser debug screenshots and excerpts independently from result JSON.
6. Emit structured error records and clear logs identifying the limit reached.
7. Document defaults and sizing guidance.

## Acceptance Criteria

- Pathological configs cannot exceed configured duration, record, or result-byte limits.
- Limit failures produce a terminal job/run state and structured error information.
- Partial files and temporary files are not left behind after rejected writes.
- Defaults preserve ordinary existing jobs.
- Settings validation rejects contradictory or nonsensical limits.

## Verification

- Add unit tests for each limit and interaction tests for pagination plus retries.
- Add an integration test proving an oversized run terminates and remains queryable.
- Add a disk-full or simulated write-failure test.
- Run Ruff, mypy for touched modules, and the full test suite.

## Non-Goals

- Streaming result APIs are a possible future design but are not required here.
- Do not rely on container OOM behavior as the enforcement mechanism.
