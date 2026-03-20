# Epic 1: Schema & Migrations — Execution Plan

## Waves

```
Wave 1 (parallel): 01, 02, 03, 04  — four independent SQL files
Wave 2 (sequential): 05             — database.py depends on all SQL files
```

## Task Summary

| Task | File | Action | Depends On |
|------|------|--------|------------|
| 01 — Rewrite jobs SQL | `sql/001_create_jobs.sql` | modify | — |
| 02 — Rewrite errors SQL | `sql/002_create_errors.sql` | modify | — |
| 03 — Rewrite results_meta SQL | `sql/003_create_results_meta.sql` | modify | — |
| 04 — Create job_runs SQL | `sql/004_create_job_runs.sql` | create | — |
| 05 — Update migration runner | `src/scrapeyard/storage/database.py` | modify | 01, 02, 03, 04 |

## Wave 1 — SQL Files (parallel)

Tasks 01 through 04 touch only `sql/*.sql` files. Each is independent: no
task reads or writes the same file as another. All four can be executed in
parallel by separate agents or in any order.

## Wave 2 — Migration Runner (sequential)

Task 05 modifies `database.py` to reference the SQL files produced in Wave 1.
It must run after all four Wave 1 tasks are complete.

## Done Criteria

- All five SQL files parse cleanly: `sqlite3 :memory: < sql/<file>.sql`
- `ruff check src/scrapeyard/storage/database.py` passes
- App starts cleanly with an empty DB dir
