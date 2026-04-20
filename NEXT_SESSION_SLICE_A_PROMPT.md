Start with Slice A from `CONCRETE_CLEANUP_PLAN.md` in this Scrapeyard repo.

Your job is to execute only Workstream 1: decompose the worker execution path. Do not touch other workstreams unless you discover a tiny prerequisite change that is strictly required to complete Slice A safely.

Read these files first, in this order:
1. `AGENTS.md`
2. `CONCRETE_CLEANUP_PLAN.md`
3. `WIDE_AUDIT_FINDINGS.md`
4. `src/scrapeyard/queue/worker.py`
5. `src/scrapeyard/queue/validation_policy.py`
6. `src/scrapeyard/engine/scraper.py`
7. The most relevant existing tests:
   - `tests/unit/test_worker_decomposition.py`
   - `tests/unit/test_worker_validation_actions.py`
   - `tests/unit/test_worker_error_handling.py`
   - `tests/unit/test_scraper_decomposition.py`
   - `tests/unit/test_scraper_failure_observability.py`
   - `tests/unit/test_scraper_pagination_helpers.py`

Objective:
Reduce change amplification in the scrape execution path by splitting orchestration code into smaller, single-purpose helpers while preserving behavior.

Scope boundaries:
- In scope:
  - `src/scrapeyard/queue/worker.py`
  - `src/scrapeyard/queue/validation_policy.py`
  - `src/scrapeyard/engine/scraper.py`
  - directly related unit tests only as needed
- Out of scope:
  - alias cleanup / test seam redesign
  - API response normalization
  - selector strict/non-strict dedup
  - storage cleanup
  - comment-only cleanup outside touched code

Required execution order:
1. Lock down current behavior with focused regression tests before refactoring.
2. Refactor `scrape_task()` into explicit stages without changing behavior.
3. Refactor `apply_validation()` into smaller policy helpers without changing behavior.
4. Refactor `scrape_target()` into clearer fetch/extract/finalize/error-handling phases without changing behavior.
5. Run the verification commands and report exactly what changed.

Non-negotiables:
- Preserve existing HTTP/runtime behavior.
- Keep the current package layout.
- Prefer extraction and consolidation over introducing new abstraction layers.
- Do not mix unrelated refactors into the same change.
- Keep crash handling and supersession checks explicit.
- Keep pagination, debug capture, and exception classification semantics unchanged.

What “done” looks like for Slice A:
- `worker.py`, `validation_policy.py`, and `scraper.py` are materially easier to scan.
- Hotspot functions are shorter and split into clear single-purpose helpers.
- Behavior is locked down by tests.
- `ruff` passes.
- Relevant unit tests pass.

Verification commands you must run:
- `poetry run ruff check src tests`
- `poetry run pytest tests/unit/test_worker_decomposition.py tests/unit/test_worker_validation_actions.py tests/unit/test_worker_error_handling.py tests/unit/test_scraper_decomposition.py tests/unit/test_scraper_failure_observability.py tests/unit/test_scraper_pagination_helpers.py -v`

If the targeted tests reveal an existing unrelated failure:
- do not sprawl into a broad cleanup
- isolate whether it blocks Slice A
- fix it only if it is a direct prerequisite
- otherwise note it explicitly and stop

Deliverables for this session:
1. The code changes for Slice A
2. Any added/updated regression tests
3. A concise summary of the helper extractions you made
4. Verification results with exact commands run
5. Any risks or follow-up items that should be tackled in Slice B rather than now
