# Repair the Live Redis Test Lane

> **Status: completed for 0.6.0.** This file is a historical design record;
> its Problem section describes the pre-implementation state. See the
> [archive index](README.md) for completion evidence.

Priority: P0

## Problem

The official live-Redis script sets `SCRAPEYARD_API_KEYS`, but its HTTP requests omit `X-API-Key`, so all three tests fail with 401. The lane also inherits the repository-wide 80% coverage threshold even though it runs only three focused tests. With those harness errors neutralized, all three tests pass.

Relevant code:

- `scripts/run_live_redis_tests.sh`
- `tests/live_redis/conftest.py`
- `tests/live_redis/test_queue_lifecycle.py`
- `pyproject.toml`
- `docker-compose.test.yml`

## Required Outcome

The documented live-Redis command must pass from a clean checkout and exercise authenticated requests through the real Redis/arq path.

## Implementation Scope

1. Make the test client send the configured API key on every protected request.
2. Override the full-suite coverage options for this focused lane while retaining timeouts and useful reporting.
3. Ensure the isolated Redis database and project resources are cleaned after success, failure, and interruption.
4. Avoid leaving named Docker volumes created solely for the test lane.
5. Fail with a clear prerequisite message when Docker or Compose is unavailable.
6. Resolve or track the arq/Redis deprecation warning observed during worker shutdown.

## Acceptance Criteria

- `./scripts/run_live_redis_tests.sh` passes all live-Redis tests without manual environment changes.
- Requests exercise enabled authentication rather than disabling it.
- The command returns nonzero for test failure and always cleans containers, networks, and test-only volumes.
- No global coverage-floor failure is emitted for the focused lane.
- The documented port is checked for conflicts or made configurable.

## Verification

- Run the script twice consecutively from a clean Docker state.
- Force one test failure and confirm cleanup still occurs.
- Interrupt the script and confirm cleanup occurs.
- Run the normal full suite afterward.

## Non-Goals

- These tests may continue mocking the external scrape target; real browser tests are covered separately.
