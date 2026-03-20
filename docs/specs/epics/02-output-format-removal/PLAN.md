# Epic 2: Output Format Removal — Execution Plan

## Wave 1 (parallel): independent deletions/modifications

| Task | Title | Action |
|------|-------|--------|
| 01 | Delete formatters directory | delete `src/scrapeyard/formatters/` |
| 02 | Simplify config schema | modify `src/scrapeyard/config/schema.py` |
| 03 | Delete formatter tests | delete `tests/unit/test_formatters.py` |

No dependencies between these three — execute in parallel.

## Wave 2 (parallel): result_store and worker cleanup

| Task | Title | Action |
|------|-------|--------|
| 04 | Strip format from result store | modify `src/scrapeyard/storage/result_store.py` |
| 05 | Strip format from worker | modify `src/scrapeyard/queue/worker.py` |

Depends on Wave 1: formatter imports and `OutputFormat` enum must be gone first.
Tasks 04 and 05 are independent of each other.

## Wave 3 (sequential): config cleanup sweep

| Task | Title | Action |
|------|-------|--------|
| 06 | Clean test configs | modify `docs/test-configs/*.yaml` + fixtures |

Depends on Wave 2: must run after schema and store changes land so tests validate correctly.
