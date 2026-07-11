# Add Versioned Database Migrations

Priority: P2

## Problem

Migration scripts are rerun at every startup and rely on manual idempotency. There is no migration ledger, recorded schema version, checksum validation, or explicit handling of partially applied future migrations. This limits safe evolution beyond `CREATE IF NOT EXISTS` operations.

Relevant code:

- `src/scrapeyard/storage/database.py`
- `sql/*.sql`
- `sql/README.md`

## Required Outcome

Each database must record exactly which ordered migrations have been successfully applied and reject altered or ambiguous migration history.

## Implementation Scope

1. Add a migration-history table to each SQLite database.
2. Record migration identifier, checksum, and applied timestamp in the same transaction as the migration where SQLite permits.
3. Apply only unapplied migrations in numeric order.
4. Fail startup on checksum drift, gaps, duplicate identifiers, or a migration assigned to the wrong database.
5. Preserve safe upgrade from existing databases with migrations `001` through `009` already reflected but no ledger.
6. Update contributor documentation with forward-only migration rules and rollback/backup expectations.

## Acceptance Criteria

- A fresh database records all applicable migrations once.
- Repeated startup performs no destructive migration work.
- Existing deployed databases can be baselined without losing data.
- Modified historical SQL fails clearly rather than silently rerunning.
- A failed migration does not appear as successfully applied.

## Verification

- Add fresh-install, existing-install, repeat-startup, checksum-drift, gap, and failure-rollback tests.
- Build wheel and sdist and verify migrations remain packaged.
- Run all database, storage, integration, and full tests.

## Non-Goals

- Automatic downgrade migrations are not required.
