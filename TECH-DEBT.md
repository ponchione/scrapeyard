# Technical Debt Register

Last updated: 2026-04-09

Resolved this session: Slices A-C (worker orchestration, scraper-engine, and route/controller cleanup).
Active slices: D-H.

This file tracks active technical debt only. Stale resolved-history detail from the
previous register has been pruned so this document stays useful as an actionable
backlog instead of a changelog.

Audit basis:
- Wide-sweep, shallow-pass code smell audit across all 40 source modules in `src/scrapeyard/`
- Static hotspot scan by file size, function size, and rough branch density
- Focused reads of worker, scraper, API, dependency-wiring, storage, webhook, and detection layers
- Tooling check: `poetry run ruff check src tests` passed cleanly
- Test inventory check: 495 tests collected

---

## Prioritization rubric

- Critical: architecture hotspots that will keep increasing change risk and defect risk
- High: design smells that materially slow maintenance or hide failures
- Medium: worthwhile cleanup that improves consistency and refactor safety
- Low: polish and future-proofing; do when already touching the area

---

## Active implementation slices

### Slice D: Failure-mode visibility and exception-swallowing hardening
Severity: High
Primary files:
- `src/scrapeyard/engine/selectors.py`
- `src/scrapeyard/engine/detection.py`
- `src/scrapeyard/engine/scraper.py`
- `src/scrapeyard/webhook/dispatcher.py`

Why this slice exists:
- Several helpers intentionally degrade failures into empty outputs or dropped debug data.
- That behavior is sometimes desirable, but today it is too silent and too broad.

Debt items:
- D1: Narrow broad `except Exception` catches where possible
  - Especially in selector execution and DOM helper paths.
- D2: Distinguish “empty match” from “selector engine failed”
  - Silent conversion to `[]` can hide integration breakage as business-level no-data.
- D3: Add low-noise debug logging for swallowed exceptions that are intentionally tolerated
  - Preserve resilience without destroying diagnosability.
- D4: Audit browser debug-capture fallbacks
  - Failed screenshot/title/content capture should be visible enough for debugging without crashing jobs.

Exit criteria:
- Intentional resilience remains, but operational failures are no longer silently indistinguishable from legitimate empty results.

### Slice E: Domain modeling consistency cleanup
Severity: Medium
Primary files:
- `src/scrapeyard/engine/scraper.py`
- `src/scrapeyard/queue/worker.py`
- `src/scrapeyard/models/job.py`

Why this slice exists:
- Job lifecycle uses enums, but per-target result state still uses raw string literals such as `"success"` and `"failed"`.
- This is a small but important consistency gap in the domain model.

Debt items:
- E1: Introduce a typed target-status model
  - Replace raw target status strings with an enum or similarly constrained type.
- E2: Remove remaining string-literal comparisons for target lifecycle
  - Strengthen refactor safety and reduce drift risk.
- E3: Align result/status modeling across job-level and target-level flows
  - Make state transitions and status checks feel uniform across the system.

Exit criteria:
- Target lifecycle is type-constrained and consistent with the rest of the domain model.

### Slice F: Health/runtime wiring separation
Severity: Medium
Primary files:
- `src/scrapeyard/main.py`
- `src/scrapeyard/api/dependencies.py`

Why this slice exists:
- `main.py` and dependency wiring are still carrying mixed concerns: app entrypoint, singleton lifecycle, health-summary aggregation, cache behavior, and runtime wiring.
- This works, but it increases hidden global-ish runtime state and test sensitivity.

Debt items:
- F1: Move health/project-summary logic into a dedicated module/service
  - Keep `main.py` focused on application assembly and lifespan order.
- F2: Reassess singleton/cache wiring in `api/dependencies.py`
  - Current `lru_cache` + holder patterns are workable but easy to grow into hidden runtime complexity.
- F3: Clarify boundary between app container concerns and DI convenience helpers
  - If runtime wiring expands, an explicit app container may age better than layered module singletons.

Exit criteria:
- Entry-point code is slimmer.
- Runtime state ownership is easier to reason about and test.

### Slice G: Storage-layer growth management
Severity: Medium
Primary files:
- `src/scrapeyard/storage/job_store.py`
- `src/scrapeyard/storage/result_store.py`
- `src/scrapeyard/storage/error_store.py`

Why this slice exists:
- Store modules are healthy today, but `job_store.py` in particular is trending toward a mini-ORM/query-service hybrid.
- Query building, row mapping, and write semantics are starting to accumulate in single files.

Debt items:
- G1: Split row-mapping helpers from query/write behavior where growth continues
- G2: Keep aggregation/reporting queries from crowding basic CRUD/store responsibilities
- G3: Establish a clearer pattern before additional admin/reporting endpoints expand the stores further

Exit criteria:
- Storage files remain readable as the query surface grows.
- Future reporting additions do not automatically enlarge the core store classes.

### Slice H: Consistency and polish backlog
Severity: Low
Primary files:
- `src/scrapeyard/api/routes.py`
- `src/scrapeyard/common/*`
- `src/scrapeyard/queue/pool.py`

Why this slice exists:
- These are not urgent defects, but they are recurring “rough edge” smells.

Debt items:
- H1: Centralize time sourcing patterns where useful
  - `datetime.now(timezone.utc)` is repeated in many modules; shared helpers would improve consistency.
- H2: Revisit Linux-specific memory-check assumptions in `queue/pool.py`
  - Current `/proc/self/statm` approach is pragmatic but infrastructure-specific inside queue code.
- H3: Continue reducing manual response/encoding boilerplate in API helpers
  - Small cleanup even after larger route-thinning work.

Exit criteria:
- Repeated small patterns are normalized when touched.
- Platform-specific logic is explicit and contained.

---

## Ranked active debt items

1. D1: Narrow broad `except Exception` catches where possible (High)
2. D2: Distinguish empty matches from selector-engine failure (High)
3. E1: Introduce a typed target-status model (Medium)
4. F1: Move health/project-summary logic into a dedicated module/service (Medium)
5. G1: Split row-mapping helpers from growing store query/write behavior (Medium)
6. H1: Centralize repeated UTC-now/time-sourcing patterns where useful (Low)

---

## Notes

- This register intentionally excludes resolved historical slices from earlier audits.
- If future work starts on any slice above, update this file by removing completed items rather than accumulating stale resolved detail.
