# Status — 2026-03-20

## What was done

All 6 epics from `docs/specs/run-model-and-api-contract.md` are implemented and merged to main:

1. **Schema & Migrations** — `job_runs` table, cleaned `jobs`/`errors`/`results_meta` schemas, multi-migration runner
2. **Output Format Removal** — `formatters/` deleted, `OutputFormat` enum removed, JSON-only
3. **Run Model Elevation** — `JobRun` model, 3 new `job_store` query methods, `ResultPayload`, `ErrorRecord.run_id`
4. **Worker Run Lifecycle** — `trigger` param threaded through enqueue path, run create/finalize/crash in worker, inline GroupBy grouping
5. **API Contract Expansion** — expanded `GET /jobs/{id}` with runs/stats/next_run_at, updated all endpoints
6. **Scheduler Integration** — `trigger="scheduled"`, `get_next_run_time()`

164 unit + 20 integration tests passing. Ruff clean. No existing DBs to nuke (fresh env).

## What's left (out of scope, separate work order)

- **New test coverage** for: `JobRun` model, `get_job_runs()` / `get_job_run_stats()` / `list_jobs_with_stats()`, `get_next_run_time()`, run lifecycle in worker (create/update/crash paths). Spec section 11 has the full list.
- **CTO review issue #3** (test coverage / CI harness) — not part of this spec.

## Spec docs

All specs and per-task breakdowns live in `docs/specs/`. They can be archived or kept as reference — the implementation matches them exactly.
