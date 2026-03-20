# Epic 4: Worker Run Lifecycle — Execution Plan

## Wave 1 (parallel): 01, 02

| Task | File | Action |
|------|------|--------|
| 01-trigger-param-pool | `src/scrapeyard/queue/pool.py` | modify |
| 02-trigger-param-dependencies | `src/scrapeyard/api/dependencies.py` | modify |

These are independent files with no overlap — safe to run in parallel.

## Wave 2 (sequential): 03

| Task | File | Action |
|------|------|--------|
| 03-worker-run-lifecycle | `src/scrapeyard/queue/worker.py` | modify |

Tasks 03 through the end all touch `worker.py`, so they are combined into a
single task file that lists the changes sequentially. Splitting `worker.py`
changes across parallel agents would cause merge conflicts.
