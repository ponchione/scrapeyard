# Add a Checked-In CI Pipeline

Priority: P1

## Problem

Deployment documentation requires a passing CI suite, but the repository contains no checked-in CI workflow. Local commands exist, yet there is no enforceable record of the Python matrix, lint/type/test gates, dependency audit, package build, or live-Redis execution.

Relevant files:

- `pyproject.toml`
- `docs/TESTING.md`
- `docs/DEPLOYMENT.md`
- `scripts/run_live_redis_tests.sh`

## Required Outcome

Every proposed change must run reproducible, visible quality gates before merge and release.

## Implementation Scope

1. Add the repository's chosen CI provider configuration.
2. Run Ruff, the configured mypy scope, full pytest with coverage, package build, and production dependency audit.
3. Add a Redis service lane for the real queue tests after repairing that runner.
4. Test the minimum supported Python version and the production Python version.
5. Cache dependencies without caching mutable application state or browser secrets.
6. Upload useful test/coverage artifacts on failure.
7. Add a scheduled dependency audit and optional container-build smoke lane.

## Acceptance Criteria

- All documented merge gates run from checked-in configuration.
- A deliberately failing lint, test, type, audit, or build step fails CI.
- Live-Redis tests run against an isolated Redis service.
- CI does not require production secrets or public deployment access.
- Branch protection can require the resulting checks.

## Verification

- Validate the workflow syntax locally where tooling permits.
- Open a test branch/PR and observe every required check.
- Confirm artifacts and logs make failures diagnosable.

## Dependencies

- Complete `12-repair-the-live-redis-test-lane.md` before making that lane required.

## Non-Goals

- This task does not publish a production deployment automatically.
