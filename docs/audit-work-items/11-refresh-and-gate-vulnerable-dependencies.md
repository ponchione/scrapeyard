# Refresh and Gate Vulnerable Dependencies

Priority: P0

## Problem

The current environment audit reports known advisories in runtime-graph packages including `aiohttp`, `idna`, `pydantic-settings`, `PyJWT`, and `Starlette`, plus additional development/tooling dependencies. The lock file therefore cannot be promoted without triage.

Relevant files:

- `pyproject.toml`
- `poetry.lock`
- `Dockerfile`

## Required Outcome

The lock must contain patched compatible versions, with automated auditing preventing known unapproved vulnerabilities from re-entering release artifacts.

## Implementation Scope

1. Refresh direct and transitive dependencies to versions that resolve current advisories.
2. Review each advisory for runtime reachability and document any temporary exception with owner, rationale, and expiry date.
3. Run the application, browser import contracts, package build, and test suite against the refreshed lock.
4. Audit the exported production dependency set separately from development tooling.
5. Add `pip-audit` to CI with an explicit, reviewed ignore mechanism rather than unconditional success.
6. Verify the pinned Poetry builder can consume the refreshed lock and export dependencies.

## Acceptance Criteria

- Production dependency audit reports no unapproved known vulnerabilities.
- Development audit reports no unapproved known vulnerabilities.
- Any exception is narrow, documented, time-bounded, and linked to an upstream issue.
- Full tests, real-Redis tests, package build, and Docker image build pass.
- Runtime browser packages remain mutually compatible.

## Verification

- Run `poetry run pip-audit` and a production-only audit from the exported requirements.
- Run Ruff, mypy, full pytest, live Redis, package build, and full Docker build.
- Smoke-test basic, dynamic, dynamic-stealth, and stealthy fetcher startup.

## Non-Goals

- Do not broadly suppress vulnerability IDs merely to make CI green.
