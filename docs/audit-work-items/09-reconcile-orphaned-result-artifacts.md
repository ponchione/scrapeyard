# Reconcile Orphaned Result Artifacts

Priority: P2

## Problem

Result files and metadata live in different systems. Atomic file replacement protects file integrity, and cleanup deletes metadata before files, but crashes can leave unindexed run directories or temporary artifacts. Existing retention only considers rows in `results_meta`, so filesystem orphans can persist indefinitely.

Relevant code:

- `src/scrapeyard/storage/result_store.py`
- `src/scrapeyard/storage/filesystem.py`
- `src/scrapeyard/storage/cleanup.py`
- `src/scrapeyard/storage/result_queries.py`

## Required Outcome

The service must detect, report, and safely remove orphaned result directories and stale temporary files without deleting valid or in-progress runs.

## Implementation Scope

1. Define the authoritative relationship between `results_meta` and `project/job/run` directories.
2. Add an orphan scan that validates all paths through existing containment checks.
3. Protect recent/in-progress directories with an age threshold and active-run check.
4. Remove stale atomic-write temporary files.
5. Detect metadata rows whose `results.json` is missing or corrupt and expose them as storage failures.
6. Add dry-run/reporting support before destructive cleanup.
7. Record scan counts, removed bytes, and failures in logs/metrics.

## Acceptance Criteria

- Unindexed stale run directories are detected and removable.
- Active or recently written directories are never removed.
- Metadata pointing to missing/corrupt files is surfaced explicitly.
- All deletion remains confined beneath `storage_results_dir`.
- Repeated cleanup is idempotent.

## Verification

- Add filesystem and result-store tests for valid, orphaned, symlinked, malformed, recent, and missing-file cases.
- Add cleanup-loop failure handling tests.
- Run storage unit tests and the full suite.

## Non-Goals

- Do not attempt content-level recovery of corrupt JSON in this task.
