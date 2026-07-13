# Add Safe Job Cancellation and Deletion

> **Status: completed for 0.6.0.** This file is a historical design record;
> its Problem section describes the pre-implementation state. See the
> [archive index](README.md) for completion evidence.

Priority: P1

## Problem

Deleting a job removes scheduler and database state but does not cancel queued or running work. A worker may continue fetching, recreate error rows after deletion, or reach persistence paths after its parent job is gone. Retaining results with `delete_results=false` also leaves data that cannot be accessed through the current API.

Relevant code:

- `src/scrapeyard/api/routes.py`
- `src/scrapeyard/queue/pool.py`
- `src/scrapeyard/queue/worker.py`
- `src/scrapeyard/storage/job_store.py`
- `src/scrapeyard/storage/error_store.py`
- `src/scrapeyard/storage/result_store.py`

## Required Outcome

Cancellation and deletion must be explicit state transitions that prevent new side effects and have documented artifact-retention semantics.

## Implementation Scope

1. Add a `cancelled` job/run status through models, SQL, serializers, and tests, or document and implement an equivalent tombstone state.
2. Add queue cancellation by `run_id` for unclaimed work.
3. Add cooperative cancellation checks between target, pagination, retry, and persistence stages.
4. Make terminal writes conditional on active ownership and non-deleted/non-cancelled state.
5. Decide whether DELETE rejects active jobs, cancels then deletes, or becomes asynchronous; expose that contract clearly.
6. Define accessible retention behavior when results are preserved.
7. Clean related errors, results, runs, and webhook rows without races.

## Acceptance Criteria

- Cancelling a queued job prevents its scrape handler from running.
- Cancelling a running job stops further target starts and persistence as soon as practical.
- Deletion cannot leave newly created error rows or webhook intents after returning success.
- Preserved results are either accessible through a documented endpoint or explicitly unsupported.
- Repeated cancel/delete requests are idempotent.

## Verification

- Add unit and integration tests for queued, running, terminal, already-deleted, and cancellation-race cases.
- Add a real-Redis cancellation test.
- Run the full suite.

## Non-Goals

- Hard-killing browser processes is only required if cooperative cancellation cannot close them reliably.
