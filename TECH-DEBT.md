# Technical Debt Register

Last updated: 2026-04-09

Resolved this session: Slices A-H (worker orchestration, scraper-engine, route/controller cleanup, failure-mode visibility hardening, typed target-status cleanup, health/runtime wiring separation, storage-layer growth management, and consistency/polish cleanup).
Active slices: none.

This file tracks active technical debt only. Stale resolved-history detail from the
previous register has been pruned so this document stays useful as an actionable
backlog instead of a changelog.

Audit basis:
- Wide-sweep, shallow-pass code smell audit across all 40 source modules in `src/scrapeyard/`
- Static hotspot scan by file size, function size, and rough branch density
- Focused reads of worker, scraper, API, dependency-wiring, storage, webhook, and detection layers
- Tooling check: `poetry run ruff check src tests` passed cleanly
- Test inventory check: 519 tests collected

---

## Prioritization rubric

- Critical: architecture hotspots that will keep increasing change risk and defect risk
- High: design smells that materially slow maintenance or hide failures
- Medium: worthwhile cleanup that improves consistency and refactor safety
- Low: polish and future-proofing; do when already touching the area

---

## Active implementation slices

### Slice G: Storage-layer growth management
Resolved 2026-04-09.

What changed:
- Split job-row mapping into `src/scrapeyard/storage/job_rows.py` and shared job SQL constants into `src/scrapeyard/storage/job_sql.py` so `job_store.py` no longer owns both CRUD/write flow and row-decoding details.
- Moved reporting/query-shaping helpers into `src/scrapeyard/storage/job_queries.py`, `src/scrapeyard/storage/error_queries.py`, and `src/scrapeyard/storage/result_queries.py`, keeping store methods focused on DB orchestration and protocol behavior.
- Added focused helper tests in `tests/unit/test_storage_job_queries.py` and `tests/unit/test_storage_error_queries.py` so future reporting/storage growth has direct coverage outside the concrete store classes.

### Slice H: Consistency and polish backlog
Resolved 2026-04-09.

What changed:
- Added `src/scrapeyard/common/time.py` and switched touched modules to `utc_now()` so repeated UTC time sourcing now goes through one shared helper instead of ad hoc `datetime.now(timezone.utc)` calls.
- Moved Linux `/proc/self/statm` parsing into `src/scrapeyard/queue/memory.py`, making `queue/pool.py` explicitly depend on a platform-aware helper instead of carrying Linux-specific process inspection inline.
- Reduced remaining API boilerplate by adding `src/scrapeyard/api/query_parsing.py` for `/errors` filter parsing/validation and `no_content_response()` in `src/scrapeyard/api/response_utils.py`, leaving `routes.py` thinner and more uniform.

---

## Ranked active debt items

None currently tracked.

---

## Notes

- This register intentionally excludes resolved historical slices from earlier audits.
- If future work starts on any slice above, update this file by removing completed items rather than accumulating stale resolved detail.
