# Make Run Finalization and Webhook Intent Recoverable

> **Status: completed for 0.6.0.** This file is a historical design record;
> its Problem section describes the pre-implementation state. See the
> [archive index](README.md) for completion evidence.

Priority: P1

## Problem

Results, errors, job/run state, filesystem artifacts, and webhook outbox rows span different transactional systems. A crash after run finalization but before webhook outbox insertion can leave a terminal run with no webhook intent. Startup can repair job status from the run but does not reconstruct the missing delivery.

Relevant code:

- `src/scrapeyard/queue/worker.py`
- `src/scrapeyard/queue/run_lifecycle.py`
- `src/scrapeyard/storage/job_store.py`
- `src/scrapeyard/storage/result_store.py`
- `src/scrapeyard/storage/webhook_outbox.py`
- `src/scrapeyard/webhook/dispatcher.py`

## Required Outcome

Every terminal run that requires a webhook must have a durable, idempotent webhook intent, even across process crashes.

## Implementation Scope

1. Derive a deterministic delivery identity from job, run, and event rather than creating a new random intent each time.
2. Persist terminal run state and webhook intent in one `jobs.db` transaction, or add a durable terminal-event table consumed into the outbox.
3. Add startup reconciliation for terminal runs that require but lack an event/delivery row.
4. Keep HTTP delivery asynchronous and independent from job completion.
5. Preserve receiver-side deduplication fields.
6. Document the consistency model for result files and cross-database error counts.

## Acceptance Criteria

- A crash at every boundary between result save, run finalization, webhook intent, and job update cannot permanently lose a required webhook.
- Reconciliation cannot create duplicate logical events.
- Webhook endpoint failure never changes the scrape result status.
- A terminal run and job eventually converge to matching status.

## Verification

- Add fault-injection tests at each persistence boundary.
- Add restart tests that reconcile intentionally incomplete lifecycle states.
- Add tests proving deterministic delivery deduplication.
- Run storage, worker, webhook, integration, and live-Redis suites.

## Non-Goals

- Exactly-once HTTP delivery is not achievable; the contract remains at-least-once with deduplication.
