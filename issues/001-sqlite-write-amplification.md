# Issue 001: SQLite Write Amplification In Worker Path

Severity: High

## Summary

The worker path opens fresh SQLite connections for many small writes, especially error logging. Under noisy failures, database overhead will become a meaningful part of job runtime.

## Evidence

- `src/scrapeyard/storage/database.py:63` opens a new `aiosqlite.connect(...)` for every `get_db()` use.
- `src/scrapeyard/queue/worker.py:166` and `src/scrapeyard/queue/worker.py:266` log errors inside per-error loops.
- `src/scrapeyard/storage/error_store.py:39` inserts one row and commits immediately for each logged error.
- `src/scrapeyard/queue/worker.py:72`, `src/scrapeyard/queue/worker.py:375`, and `src/scrapeyard/queue/worker.py:443` perform additional standalone run-lifecycle writes.

## Why It Matters

- Failure-heavy jobs will spend unnecessary time opening SQLite connections and committing many tiny transactions.
- This increases lock churn and reduces throughput as concurrent jobs rise.
- The design will scale poorly if the service hits a bad target set, proxy outage, or anti-bot wave.

## Recommendation

- Reuse long-lived SQLite connections per database, or introduce a small connection manager instead of opening per operation.
- Batch error inserts per run, or at least per target, and commit once.
- Collapse run-lifecycle updates into fewer transactions where possible.

## Deployment Risk

Moderate to high for production traffic with concurrent runs and non-trivial error volume.

## Initial Plan

### Goal

Reduce SQLite connection churn and tiny write transactions in the worker hot path without changing external API behavior or storage schema.

### Scope Boundaries

- Keep this issue focused on connection/transaction overhead in the worker path.
- Do not fold in the broader `jobs` row-shape work from `002-job-update-full-row-rewrites.md`, except where a small helper is needed to group existing writes.
- Do not combine this change with the blocking filesystem work tracked in `003-blocking-filesystem-on-event-loop.md`.

### Proposed Approach

1. Add an explicit write-transaction helper in `src/scrapeyard/storage/database.py`.
   - Keep the current read path simple.
   - Introduce a small writer/session helper so a call site can reuse one SQLite connection across related statements and commit once.
   - Make shutdown/test teardown close any cached writer state cleanly.
2. Batch error inserts in `src/scrapeyard/storage/error_store.py`.
   - Add a batch insert method that uses `executemany(...)` in one transaction.
   - Keep `log_error(...)` as a thin compatibility wrapper around the batch path.
3. Refactor `src/scrapeyard/queue/worker.py` to flush errors in batches.
   - Collect `ErrorRecord` instances per target or retry branch instead of committing per error string.
   - Flush the batch once after the target outcome is known so noisy failures stop causing one transaction per message.
4. Collapse run-lifecycle writes where it is low-risk to do so.
   - Reuse the same `jobs.db` writer for run start writes.
   - Reuse the same `jobs.db` writer for run finalization / crash updates.
   - Keep lifecycle behavior identical; do not mix in a wider `JobStore` redesign in this patch.
5. Add regression coverage for connection reuse and batching.
   - Extend database tests to cover writer lifecycle and teardown.
   - Extend error store tests to cover batch insert behavior.
   - Add worker tests for multi-error failure paths so they assert batched persistence rather than per-error commits.

### Key Decisions

- Reusing SQLite connections is only safe if transaction ownership is explicit. A shared cached connection with call-site-managed `commit()` calls would be too easy to interleave incorrectly.
- Error persistence should be batched per target or retry block, not deferred until the very end of the run, so crash-time observability is preserved.
- If the connection-manager portion adds too much complexity, ship error batching first and keep run-lifecycle reuse as a narrow follow-up patch under the same issue.

### Verification

- `poetry run pytest tests/unit/test_database.py tests/unit/test_database_reset.py`
- `poetry run pytest tests/unit/test_error_store.py`
- `poetry run pytest tests/unit/test_worker_error_handling.py tests/unit/test_worker_run_lifecycle.py`
- `poetry run pytest tests/integration/test_scrape_lifecycle.py tests/integration/test_run_model_api.py`

### Definition of Done

- Failure-heavy targets no longer cause one `errors.db` transaction per logged error message.
- Worker run start/finalization no longer pay fresh write-connection setup for each individual statement in the hot path.
- Existing API responses, schema, and run lifecycle semantics remain unchanged.
