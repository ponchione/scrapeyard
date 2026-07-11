# Modernize Project Metadata and Build Tooling

Priority: P3

## Problem

`poetry check` succeeds but reports deprecation warnings for legacy `[tool.poetry]` metadata. The Docker builder pins Poetry 1.8.5 while local tooling may be newer, creating an avoidable difference in lock/export behavior. Reproducible production-only dependency auditing is also awkward when the export command is unavailable locally.

Relevant files:

- `pyproject.toml`
- `poetry.lock`
- `Dockerfile`
- `docs/TESTING.md`

## Required Outcome

Local development, CI, and Docker builds must use a documented compatible packaging toolchain with warning-free metadata and reproducible production dependency export.

## Implementation Scope

1. Migrate static package metadata to supported PEP 621 `[project]` fields while preserving Poetry package/source configuration as needed.
2. Choose and pin a supported Poetry version and required plugins consistently across local docs, CI, and Docker.
3. Regenerate and verify the lock with that toolchain.
4. Add a documented production-only dependency export/audit command.
5. Verify wheel/sdist layout, version metadata, SQL migration inclusion, and Docker installation.
6. Add a toolchain-version check to CI.

## Acceptance Criteria

- `poetry check` is warning-free.
- The same declared toolchain works locally, in CI, and in the Docker builder.
- Production requirements export is deterministic and auditable.
- Wheel and sdist contain the package and every SQL migration.
- Application and package versions remain synchronized from one source of truth.

## Verification

- Run metadata validation, lock consistency, package build, archive inspection, installation smoke test, and Docker build.
- Run Ruff, mypy, and the full test suite after migration.

## Non-Goals

- Do not change the application dependency set except where required for tooling compatibility or security fixes.
