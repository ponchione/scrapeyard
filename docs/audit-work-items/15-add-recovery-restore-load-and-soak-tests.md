# Add Recovery, Restore, Load, and Soak Tests

Priority: P1

## Problem

The current suite does not validate process termination during queued/running work, restored SQLite plus artifact backups, sustained queue pressure, disk pressure, or long-duration scheduler/webhook behavior. These are the principal risks in a stateful single-instance service.

Relevant areas:

- application lifespan and stale recovery
- Redis persistence and queue execution
- all three SQLite databases
- result and adaptive directories
- scheduler and webhook outbox

## Required Outcome

Release qualification must include repeatable failure-recovery and capacity tests with documented expectations.

## Implementation Scope

1. Add restart scenarios at enqueue, run creation, target execution, result write, run finalization, webhook intent, and job completion boundaries.
2. Back up and restore all SQLite databases plus results, then verify known jobs/runs/results/errors.
3. Exercise Redis restart with queued and running jobs under the documented persistence policy.
4. Add load tests for concurrent basic jobs, concurrent browser targets, large result sets, and API reads.
5. Add a soak test for scheduler firing, retention cleanup, and webhook retries.
6. Measure memory, CPU, disk growth, latency, queue depth, and recovery time.
7. Define pass/fail thresholds sized for the intended host.

## Acceptance Criteria

- Every documented crash point converges to a known terminal or recoverable state.
- Backup restoration serves a known job and result exactly as documented.
- Load cannot bypass configured job/browser/output limits.
- Soak testing shows bounded database, filesystem, task, and memory growth.
- Results are recorded in a repeatable runbook.

## Verification

- Add automated integration scenarios where practical and a documented longer-running lane for the rest.
- Run tests on the same container/runtime class intended for deployment.

## Dependencies

- Complete browser concurrency, run heartbeat, queue reconciliation, and bounded webhook work first so thresholds reflect the intended design.

## Non-Goals

- This task does not establish horizontal-scaling support.
