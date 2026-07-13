# Enforce Single-Process Runtime Assumptions

> **Status: completed for 0.6.0.** This file is a historical design record;
> its Problem section describes the pre-implementation state. See the
> [archive index](README.md) for completion evidence.

Priority: P2

## Problem

The API process embeds both arq workers and APScheduler while using local SQLite connections, local filesystem artifacts, and in-memory counters/circuit-breaker state. Running multiple Uvicorn workers or application replicas would create multiple schedulers, fragmented health counters, shared-file contention, and semantics the codebase does not currently support.

Relevant code:

- `src/scrapeyard/main.py`
- `src/scrapeyard/api/dependencies.py`
- `src/scrapeyard/queue/pool.py`
- `src/scrapeyard/scheduler/cron.py`
- `README.md`
- `docs/DEPLOYMENT.md`

## Required Outcome

Unsupported multi-process or multi-replica operation must fail clearly or be prevented by deployment configuration, while the future scaling boundary is documented.

## Implementation Scope

1. Add a startup lock or equivalent single-active-instance guard scoped to the shared data directory/Redis deployment identity.
2. Detect common multi-worker configurations and emit a fatal, actionable message.
3. Ensure Compose and deployment examples explicitly configure one application process and one replica.
4. Document which state is process-local, Redis-shared, SQLite-shared, and filesystem-shared.
5. Write an architectural note describing the changes required to split API, scheduler, and workers for future scaling.

## Acceptance Criteria

- Starting a second instance against the same single-instance data/queue identity is rejected or safely held inactive.
- Normal restart can acquire the guard after shutdown or crash recovery.
- Deployment manifests cannot accidentally start multiple Uvicorn workers.
- Documentation clearly distinguishes current support from a future distributed topology.

## Verification

- Add startup-lock acquisition, contention, stale-lock, and shutdown tests.
- Attempt two local instances against the same data and Redis and verify the documented behavior.
- Run lifespan, integration, and full suites.

## Non-Goals

- This task does not implement horizontal scaling.
