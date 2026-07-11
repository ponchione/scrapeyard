# Safe Refactor Completion Handoff

Last updated: 2026-07-11

## Status

The July 2026 safe-refactor program is complete. The implementation plan and
safety constraints are recorded in `docs/SAFE_REFACTOR_PLAN.md`. All work is
committed locally on `main`; nothing has been pushed.

The pre-existing reliability work for audit items 01-09 was first preserved as
an explicit checkpoint (`70134c6`). The refactor program then landed in
independent, reviewable slices:

| Commit | Slice |
|---|---|
| `e8d1cb8` | Add checksummed, per-database migration ledgers and forward-only migrations. |
| `026d64d` | Bound terminal webhook reconciliation with a durable run marker. |
| `a72305b` | Index and narrow destructive artifact ownership rechecks. |
| `21e5242` | Centralize cancellation-safe SQLite transactions. |
| `70519a1` | Chunk result-metadata deletion below SQLite variable limits. |
| `13220a4` | Remove obsolete and unsafe alternate lifecycle APIs. |
| `d02bdf9` | Scope run-stat aggregation before project-filtered job listing. |
| `65f0656` | Validate each basic-fetch redirect target once before request. |
| `cc3506a` | Align integration assertions with bounded reconciliation behavior. |
| `31e2d96` | Repair and harden the authenticated live-Redis verification lane. |

## Resulting invariants

- Each SQLite database records migration filename, SHA-256 checksum, and
  application time. Startup rejects history gaps, assignment drift, checksum
  drift, and partial migrations; known pre-ledger schemas are reflected before
  baselining.
- Terminal webhook recovery reads only unresolved runs or terminal runs whose
  parent still needs convergence. Intent creation and reconciliation marking
  remain atomic and deterministic.
- Artifact race checks use the `(project, run_id)` index before exact safe-path
  comparison. Symlink, active-run, grace-period, and final recheck defenses are
  unchanged.
- Shared database transactions roll back on every `BaseException`, including
  cancellation, and tolerate deliberate compare-and-set early rollbacks.
- Result metadata IDs are deleted in batches of 500 while preserving the
  intentionally different retention and lifecycle deletion orderings.
- Generic whole-job mutation, unpaginated store listing, non-atomic webhook
  submission/retry, and obsolete failure wrappers no longer remain as alternate
  production paths.
- Project-filtered job statistics constrain the run aggregation before grouping.
- Basic redirects are validated exactly once immediately before each fetch;
  final response and browser URL defenses remain in place.
- The documented live-Redis runner authenticates requests, avoids the global
  coverage floor for its focused lane, supports an overridable port, checks
  prerequisites, and removes its containers, network, and volumes.

## Verification

The final committed implementation passed:

```text
poetry run ruff check src tests
  All checks passed.

poetry run mypy src
  Success: no issues found in 77 source files.

poetry run pytest tests/unit
  1,036 passed.

poetry run pytest --no-cov tests/integration
  59 passed.

poetry run pytest
  1,095 passed, 8 skipped, 89.37% coverage.

./scripts/run_live_redis_tests.sh
  8 passed on two consecutive default-port runs.

SCRAPEYARD_TEST_REDIS_PORT=56380 ./scripts/run_live_redis_tests.sh
  8 passed.
```

The live-Redis runner left no container, network, volume, or listening test
port after each run. `git diff --check` also passed throughout the slices.

## Known limit

The live-Redis lane emits an upstream `arq` deprecation warning because
`arq.worker.Worker.close()` calls the deprecated Redis `close()` alias
internally. Scrapeyard uses the public worker shutdown API; replacing that
third-party implementation detail locally would be a riskier change than the
warning warrants. It remains non-fatal and is visible in test output for future
dependency upgrades.
