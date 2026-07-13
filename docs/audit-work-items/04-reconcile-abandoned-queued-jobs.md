# Reconcile Abandoned Queued Jobs

> **Status: completed for 0.6.0.** This file is a historical design record;
> its Problem section describes the pre-implementation state. See the
> [archive index](README.md) for completion evidence.

Priority: P1

## Problem

Startup recovery handles stale `running` jobs but not `queued` jobs whose Redis entry has disappeared. An ad-hoc job can remain queued forever after an acknowledged enqueue is lost, expired, or removed. Scheduled jobs may eventually receive another trigger, but ad-hoc jobs have no recovery source.

Relevant code:

- `src/scrapeyard/main.py`
- `src/scrapeyard/api/scrape_submission.py`
- `src/scrapeyard/queue/pool.py`
- `src/scrapeyard/storage/job_store.py`
- `src/scrapeyard/scheduler/cron.py`

## Required Outcome

Queued state in SQLite and Redis must be reconcilable after restart and observable when automatic recovery is impossible.

## Implementation Scope

1. Add a store query for stale queued jobs with a non-null `current_run_id`.
2. Add a queue inspection method that determines whether the corresponding arq job still exists.
3. Define separate recovery policy for scheduled and ad-hoc jobs: re-enqueue safely, or mark failed with an explicit recovery reason.
4. Preserve the original `run_id` when safe so duplicate queue records remain idempotent.
5. Run reconciliation after Redis connects but before the scheduler begins firing.
6. Record recovery counts and failures in logs and health/metrics.

## Acceptance Criteria

- A stale SQLite queued row with no Redis job cannot remain queued indefinitely.
- Existing Redis jobs are not duplicated.
- Repeated reconciliation is idempotent.
- Recovery does not race scheduler registration or a worker claiming a job.
- Ad-hoc and scheduled policies are documented.

## Verification

- Add unit tests for queue-present, queue-missing, queue-unavailable, and race cases.
- Add a live-Redis restart/loss integration test.
- Run the full suite and the live-Redis lane.

## Non-Goals

- This task does not guarantee zero job loss under every Redis persistence configuration; it makes loss detectable and recoverable.
