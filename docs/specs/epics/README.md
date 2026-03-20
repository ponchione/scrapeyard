# Epics: Run Model Elevation & API Contract Cleanup

Decomposition of [`docs/specs/run-model-and-api-contract.md`](../run-model-and-api-contract.md)
into six implementation epics.

## Dependency Graph

```
Epic 1: Schema & Migrations          (no deps — do first)
    │
    ├── Epic 2: Output Format Removal     (depends on 1)
    │       │
    ├── Epic 3: Run Model Elevation       (depends on 1)
    │       │
    │       ├── Epic 4: Worker Run Lifecycle   (depends on 2, 3)
    │       │       │
    │       │       ├── Epic 5: API Contract Expansion (depends on 3, 4, 6)
    │       │       │
    │       └───────┤
    │               │
    └── Epic 6: Scheduler Integration     (depends on 4)
```

## Execution Order

Epics 2, 3, and 6 can be parallelized once Epic 1 is complete.
Epic 4 requires 2 and 3. Epic 5 requires 3, 4, and 6.

Recommended serial order: **1 → 2 → 3 → 6 → 4 → 5**

## Epic Summary

| # | Epic | Dir | Tasks | Parallel Waves | Key Deliverable |
|---|---|---|---|---|---|
| 1 | Schema & Migrations | `01-schema-and-migrations/` | 5 | 2 | Four clean SQL files, migration runner updated |
| 2 | Output Format Removal | `02-output-format-removal/` | 6 | 3 | `formatters/` deleted, `OutputConfig` simplified |
| 3 | Run Model Elevation | `03-run-model-elevation/` | 7 | 3 | `JobRun` model, storage query methods, test updates |
| 4 | Worker Run Lifecycle | `04-worker-run-lifecycle/` | 3 | 2 | `trigger` param, run create/finalize in worker |
| 5 | API Contract Expansion | `05-api-contract-expansion/` | 5 | 1 | All route handlers serving new contract |
| 6 | Scheduler Integration | `06-scheduler-integration/` | 2 | 1 | `get_next_run_time()`, scheduled trigger |

**Total: 28 task files across 6 epics**

## Per-Epic Structure

Each epic directory contains:
- `EPIC.md` — scope, goal, acceptance criteria, files touched
- `PLAN.md` — parallel execution waves with dependency table
- `NN-task-name.md` — individual task files (one per atomic unit of work)

Tasks that touch the same file are grouped into a single task file to avoid
merge conflicts during parallel execution.
