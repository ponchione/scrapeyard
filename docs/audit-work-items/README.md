# Reliability Audit Work-Item Archive

Status: completed

The 25 numbered documents in this directory are retained as historical design
records. Their Problem sections describe Scrapeyard before the 0.6.0
reliability and production-hardening work; they are not an active backlog.

## Completion evidence

| Items | Outcome | Primary evidence |
|---|---|---|
| 01-09 | Runtime budgets, leases, recovery, cancellation, priority, and artifact reconciliation | Reliability implementation commit `70134c6` and its focused tests |
| 10 | Checksummed, forward-only SQLite migration ledgers | Migration commit `e8d1cb8`, `sql/README.md`, and database tests |
| 11-15 | Dependency gates, live Redis, CI, browser/container smoke, and release qualification | Release-hardening commit `f262f4b` and checked-in workflows/scripts |
| 16-25 | Idempotency, schedule management, API contracts, scoped authentication, encrypted secrets, metrics/readiness, deployment hardening, full-source typing, packaging, and single-instance enforcement | API/runtime hardening commit `d76fd40`, the 0.6.0 changelog, and qualification tests |

Subsequent commits on `main` refine these invariants. Dependency auditing is
continuous rather than a one-time task; commit `48cf0ab` refreshed the lock
again on 2026-07-13 after a new Click advisory appeared.

New work should be documented in a current issue, plan, or handoff rather than
by interpreting the historical Problem sections below as present-tense state.
