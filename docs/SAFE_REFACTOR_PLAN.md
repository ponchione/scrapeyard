# Safe Refactor Implementation Plan

This plan resolves the refactor findings from the July 2026 wide-sweep audit.
The work is ordered so that schema-backed optimizations build on a safe migration
foundation and every slice can be reviewed, reverted, and verified independently.

## Safety rules

- Preserve the `src/` package layout and the queue-backed sync scrape path.
- Preserve compare-and-set ownership checks, transaction boundaries, cancellation
  propagation, SSRF checks, symlink defenses, and fail-closed recovery behavior.
- Do not cache filesystem or database snapshots across a destructive race check.
- Keep SQL definitions, storage implementations, protocols, documentation, and
  tests synchronized whenever a schema or persistence contract changes.
- Commit only after the focused tests, Ruff, mypy, and `git diff --check` pass.
- Finish with the complete unit, integration, and standard test lanes.

## Slice 1: versioned database migrations

Replace unconditional replay of every SQL file with a per-database migration
ledger containing migration ID, SHA-256 checksum, and application timestamp.

Implementation requirements:

- Discover numeric SQL migrations deterministically and reject duplicate IDs,
  gaps within each database's assigned history, assignment drift, and checksum
  drift.
- Apply each migration and its ledger record in one transaction.
- Safely baseline an existing database whose schema reflects migrations 001-009
  but has no ledger, without rerunning destructive historical statements.
- Retain explicit compatibility validation for required legacy columns while
  moving future schema changes into forward-only SQL migrations.
- Verify fresh install, repeat startup, legacy baseline, changed checksum,
  missing migration, wrong database assignment, and failed migration rollback.

## Slice 2: bounded terminal-webhook reconciliation

Add a durable reconciliation marker to `job_runs`. Terminal finalization records
the marker atomically when webhook applicability is already known. Startup loads
only runs whose terminal intent decision is unresolved or whose current parent
status still needs convergence.

Safety requirements:

- A required webhook intent and the reconciliation marker commit atomically.
- A crash before the decision commits leaves the run eligible for startup repair.
- Runs with no applicable webhook are marked reconciled without creating a fake
  outbox row.
- Existing delivered, failed, and scrubbed logical intents remain terminal and
  are not recreated.
- Parent/run convergence and deterministic delivery deduplication remain intact.

## Slice 3: indexed artifact-ownership rechecks

Index the existing `(project, run_id)` artifact identity in `results_meta`.
Replace the per-candidate full-table metadata scan immediately before destructive
orphan removal with a narrowed lookup, then retain exact path normalization over
the small candidate set.

Safety requirements:

- Existing and legacy metadata rows participate without a backfill or format
  change.
- The final metadata check remains immediately before the active-run recheck and
  removal. Database errors continue to skip deletion.
- Symlink, path-depth, grace-period, and active-run protections remain unchanged.

## Slice 4: shared cancellation-safe SQLite transactions

Introduce a small async transaction context manager that supports `BEGIN
IMMEDIATE`, commits only an active transaction on normal exit, and rolls back on
all `BaseException` paths, including task cancellation.

Adopt it in job lifecycle operations and error batch writes while preserving
explicit early rollbacks for compare-and-set no-op outcomes. Add direct tests for
commit, exception rollback, cancellation rollback, and already-rolled-back exits.

## Slice 5: bounded result-metadata deletion

Centralize result metadata ID deletion and execute it in conservative chunks so
cleanup does not depend on the host SQLite variable limit.

Retain the two intentional orderings:

- ordinary retention: metadata first, filesystem second;
- explicit lifecycle deletion: filesystem first, metadata second.

Verify empty, single-row, multi-chunk, partial filesystem failure, and retry paths.

## Slice 6: remove obsolete alternate APIs

Remove production interfaces that have no production callers and are superseded
by safer paths:

- generic whole-job mutation/status/schedule updates and the unpaginated job list;
- direct in-memory webhook retry dispatch and non-atomic dispatcher submission;
- the webhook permanent-failure compatibility wrapper and no-op terminal-update
  pass-through.

Tests that need impossible/corrupt persisted states should use explicit fixture
helpers rather than keeping unsafe production mutations. Keep `send_once()` as an
internal durable-worker operation and keep `notify()` as the worker-facing
protocol.

## Slice 7: project-filtered job statistics

When `/jobs` filters by project, constrain the job-run aggregation to that project
before grouping. Preserve ordering, pagination, zero-run jobs, and the unfiltered
query. Verify query plans use the project and `(job_id, started_at)` indexes.

## Slice 8: single-validation redirect handling

Validate each basic-fetch URL exactly once immediately before its request. Remove
the duplicate destination validation between redirect iterations while retaining
final-response URL validation and browser redirect defenses. Add tests that count
validation calls and prove a private redirect target is rejected before fetch.

## Completion gates

For every slice:

```text
poetry run ruff check src tests
poetry run mypy src
focused pytest selection for the slice
git diff --check
```

Final gates:

```text
poetry run pytest tests/unit
poetry run pytest tests/integration
poetry run pytest
```

The work is complete only when each finding is represented by a dedicated commit,
the final tree is clean, migration/recovery invariants are covered directly, and
all final gates pass.
