# Add Production Metrics and Comprehensive Readiness

> **Status: completed for 0.6.0.** This file is a historical design record;
> its Problem section describes the pre-implementation state. See the
> [archive index](README.md) for completion evidence.

Priority: P1

## Problem

JSON logs and basic health probes are useful, but there is no metrics surface for queue depth, latency, retries, output size, result cleanup, scheduler health, or webhook backlog. Readiness probes only one SQLite database and the current browser count is not reliable. Operators cannot distinguish saturation, stuck work, or growing durable backlogs quickly.

Relevant code:

- `src/scrapeyard/main.py`
- `src/scrapeyard/runtime/health.py`
- `src/scrapeyard/queue/pool.py`
- `src/scrapeyard/scheduler/cron.py`
- `src/scrapeyard/storage/cleanup.py`
- `src/scrapeyard/webhook/dispatcher.py`

## Required Outcome

Operators must have bounded-cost metrics and readiness signals covering every durable subsystem and major work stage.

## Implementation Scope

1. Add a metrics exporter compatible with the deployment monitoring stack.
2. Measure API requests, queue depth/age, active jobs/targets/browsers, run duration/status, target duration/status, retries, records/bytes, and rate-limit waits.
3. Measure scheduler last-success time, cleanup last-success time, and webhook counts/backlog/oldest age.
4. Probe all SQLite databases and a safe result-directory read/write operation in readiness.
5. Detect failed/stopped background tasks for worker, scheduler, cleanup, and webhook dispatch.
6. Keep liveness cheap and separate from detailed readiness.
7. Avoid high-cardinality labels such as raw URL, job ID, or run ID.

## Acceptance Criteria

- Dashboards can show saturation, failure rates, queue age, webhook backlog, disk pressure, and scheduler/cleanup health.
- Readiness fails when any required database or artifact storage is unusable.
- Metrics remain bounded as projects/jobs/URLs grow.
- Health accurately reports per-target browser activity after browser-limit correction.
- Probe calls have explicit short timeouts.

## Verification

- Add metric emission and probe failure tests.
- Add a background-task failure test.
- Load-test metrics cardinality and endpoint cost.
- Update deployment monitoring and alert guidance.

## Dependencies

- Coordinate with browser-concurrency and webhook-retry work so the reported counters reflect final semantics.
