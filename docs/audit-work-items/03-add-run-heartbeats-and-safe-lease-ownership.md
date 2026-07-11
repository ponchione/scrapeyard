# Add Run Heartbeats and Safe Lease Ownership

Priority: P0

## Problem

A running job updates its timestamp when execution begins but does not refresh it. The scheduler uses that timestamp as a lease and can declare a legitimate long-running scrape stale after the default five minutes. It can then fail the old run, enqueue a replacement, and supersede work that is still executing.

Relevant code:

- `src/scrapeyard/queue/job_state.py`
- `src/scrapeyard/queue/worker.py`
- `src/scrapeyard/queue/run_lifecycle.py`
- `src/scrapeyard/scheduler/cron.py`
- `src/scrapeyard/storage/job_store.py`
- `src/scrapeyard/common/settings.py`

## Required Outcome

Only genuinely abandoned runs may be recovered or superseded. Lease ownership must be tied to `run_id`, refreshed during work, and protected from stale writers.

## Implementation Scope

1. Add a heartbeat timestamp or explicit lease-expiry field to the run model and SQL schema.
2. Refresh the heartbeat periodically while a run is active.
3. Make heartbeat and terminal updates conditional on the expected `run_id` and active status.
4. Separate queued-claim timeout from running heartbeat timeout.
5. Change scheduler overlap checks to use the run heartbeat rather than the job's original start time.
6. Ensure shutdown cancellation stops heartbeat activity and leaves recovery deterministic.
7. Define behavior when a heartbeat write temporarily fails.

## Acceptance Criteria

- A run longer than the lease interval remains active while heartbeats succeed.
- A scheduler trigger never supersedes a healthy active run.
- A crashed process leaves a run that becomes recoverable after the configured timeout.
- A stale worker cannot finalize or heartbeat a superseded run.
- Recovery is idempotent across repeated startup attempts.

## Verification

- Add time-controlled unit tests for healthy, stale, superseded, and write-failure cases.
- Add an integration test with a run spanning multiple heartbeat intervals.
- Add restart recovery coverage against real SQLite and Redis where practical.
- Keep SQL, storage protocols, models, and tests synchronized.

## Non-Goals

- Do not add multi-instance support beyond the compare-and-set semantics needed for correctness.
