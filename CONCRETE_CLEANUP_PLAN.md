# Scrapeyard Cleanup Plan

> For Hermes: execute this in narrow slices, one workstream at a time. Do not mix unrelated refactors into the same change.

Goal:
Turn the wide audit into an ordered cleanup plan that reduces maintainability risk without changing Scrapeyard behavior.

Architecture:
Favor narrow internal refactors over surface redesign. Keep the current package layout and runtime contracts intact, but shrink hotspot functions, remove compatibility clutter, and centralize repeated patterns behind small helpers.

Tech stack:
Python 3.10+, FastAPI, Scrapling, SQLite/aiosqlite, pytest, ruff.

---

## Ground rules

- Preserve existing HTTP/API behavior unless a task explicitly says otherwise.
- Prefer extraction and consolidation over new abstraction layers.
- Keep each PR/work slice independently reviewable.
- After every slice, run at least:
  - `poetry run ruff check src tests`
  - targeted `pytest` for touched modules
- Before merging any larger slice, run:
  - `poetry run pytest`

---

## Workstream 1: Decompose the worker execution path

Priority: Highest
Risk: Medium
Payoff: Highest
Files:
- `src/scrapeyard/queue/worker.py`
- `src/scrapeyard/queue/validation_policy.py`
- `src/scrapeyard/engine/scraper.py`
- related tests under `tests/unit/`

### Objective
Reduce change amplification in the scrape execution path by splitting orchestration code into smaller single-purpose helpers while preserving behavior.

### Task 1.1: Lock down current worker-path behavior with focused regression tests
Files:
- Modify: `tests/unit/test_worker_decomposition.py`
- Modify: `tests/unit/test_worker_validation_actions.py`
- Modify: `tests/unit/test_worker_error_handling.py`
- Modify: `tests/unit/test_scraper_decomposition.py`

Steps:
1. Add/expand tests around these exact behaviors:
   - superseded-run early exit behavior
   - all-or-nothing final status and flat-data clearing
   - output formatting shape for grouped vs merged results
   - validation retry/warn/skip/fail branching
   - selector-engine failure vs fetch failure classification
2. Run targeted tests:
   - `poetry run pytest tests/unit/test_worker_decomposition.py tests/unit/test_worker_validation_actions.py tests/unit/test_worker_error_handling.py tests/unit/test_scraper_decomposition.py -v`
3. Confirm they pass before refactoring.

### Task 1.2: Split `scrape_task()` into explicit stages
Files:
- Modify: `src/scrapeyard/queue/worker.py`
- Test: `tests/unit/test_worker_decomposition.py`

Target extraction shape:
- `_load_job_execution_context(...)`
- `_process_job_targets(...)`
- `_persist_job_results(...)`
- `_finalize_job_execution(...)`

Notes:
- Do not change call order.
- Keep crash handling in one outer boundary.
- Keep supersession checks explicit and easy to read.

Verification:
- `poetry run pytest tests/unit/test_worker_decomposition.py tests/unit/test_worker_error_handling.py -v`
- `poetry run ruff check src tests`

### Task 1.3: Break `apply_validation()` into policy helpers
Files:
- Modify: `src/scrapeyard/queue/validation_policy.py`
- Test: `tests/unit/test_worker_validation_actions.py`
- Test: `tests/unit/test_validation_policy.py`

Target extraction shape:
- `_record_validation_failure(...)`
- `_handle_warn_action(...)`
- `_handle_skip_action(...)`
- `_handle_fail_action(...)`
- `_retry_after_validation_failure(...)`
- `_build_validation_failed_result(...)`

Notes:
- Keep only one public orchestration entrypoint: `apply_validation()`.
- Remove repeated `build_error_record(...)` argument bundles where possible.

Verification:
- `poetry run pytest tests/unit/test_worker_validation_actions.py tests/unit/test_validation_policy.py -v`

### Task 1.4: Shrink `scrape_target()` and separate fetch/extract/finalize phases
Files:
- Modify: `src/scrapeyard/engine/scraper.py`
- Test: `tests/unit/test_scraper_decomposition.py`
- Test: `tests/unit/test_scraper_failure_observability.py`
- Test: `tests/unit/test_scraper_pagination_helpers.py`

Target extraction shape:
- `_prepare_scrape_context(...)`
- `_scrape_first_page(...)`
- `_scrape_paginated_pages(...)`
- `_handle_selector_execution_failure(...)`
- `_handle_scrape_exception(...)`

Notes:
- Keep exception classification semantics unchanged.
- Keep pagination and debug capture behavior exactly as today.

Verification:
- `poetry run pytest tests/unit/test_scraper_decomposition.py tests/unit/test_scraper_failure_observability.py tests/unit/test_scraper_pagination_helpers.py -v`

Definition of done for Workstream 1:
- `worker.py`, `validation_policy.py`, and `scraper.py` are each visibly shorter and easier to scan.
- No behavior regressions in worker/scraper tests.

---

## Workstream 2: Remove alias-based compatibility clutter

Priority: High
Risk: Low to Medium
Payoff: High
Files:
- `src/scrapeyard/queue/worker.py`
- `src/scrapeyard/engine/scraper.py`
- tests that patch underscore aliases

### Objective
Stop carrying dead-feeling production aliases solely for test patching.

### Task 2.1: Find which tests rely on underscore aliases
Files:
- Search in: `tests/unit/**/*.py`

Steps:
1. Search for the alias names from `worker.py` and `scraper.py`.
2. Build a small mapping of test -> alias dependency.
3. Confirm whether each use can patch the real imported symbol instead.

Verification:
- Save the mapping in the PR description or a temporary local note.

### Task 2.2: Update tests to patch real helper names or module seams
Files:
- Modify affected tests under `tests/unit/`

Notes:
- Prefer patching the imported symbol actually used by the function under test.
- If a seam is truly needed, create one explicit helper/module seam instead of many bottom-of-file aliases.

Verification:
- Run only the affected tests first.

### Task 2.3: Delete the alias blocks
Files:
- Modify: `src/scrapeyard/queue/worker.py`
- Modify: `src/scrapeyard/engine/scraper.py`

Verification:
- `poetry run pytest tests/unit/test_worker_decomposition.py tests/unit/test_worker_error_handling.py tests/unit/test_scraper_decomposition.py -v`
- `poetry run ruff check src tests`

Definition of done for Workstream 2:
- No bottom-of-file compatibility alias blocks remain unless one is explicitly justified.
- Tests patch supported seams directly.

---

## Workstream 3: Normalize API error and response shaping

Priority: High
Risk: Medium
Payoff: Medium to High
Files:
- `src/scrapeyard/api/routes.py`
- `src/scrapeyard/api/query_parsing.py`
- `src/scrapeyard/api/response_utils.py`
- `src/scrapeyard/api/serializers.py`
- API/integration tests

### Objective
Reduce repetitive route-level response plumbing and make error handling more consistent.

### Task 3.1: Centralize repeated route error patterns
Candidate helpers:
- `not_found_error(resource: str, identifier: str) -> JSONResponse`
- `bad_request_error(message: str) -> JSONResponse`
- `conflict_error(message: str) -> JSONResponse`

Notes:
- Keep payload shapes exactly the same unless tests already allow more structure.
- Do not create an elaborate exception framework.

### Task 3.2: Centralize pagination header application + overfetch trimming
Candidate helper:
- `apply_paginated_list_response(response, rows, limit, offset)` returning `(visible_rows, has_more)`

Notes:
- Use this for `/jobs` and `/errors`.
- Preserve existing header names.

### Task 3.3: Move YAML request parsing and validation failure shaping behind one helper boundary
Current seed:
- `_read_valid_yaml_config()` already exists.

Plan:
- Keep it, but make it the only place responsible for request-body decode + config error shaping.
- Narrow broad exception handling if possible to config parse/validation errors only.

### Task 3.4: Reduce route-local payload assembly
Plan:
- Keep serializer module as the place that builds payload dicts.
- Move remaining route-local response payload fragments into serializer/helpers where it improves reuse.

Verification:
- `poetry run pytest tests/unit/test_api_query_parsing.py tests/unit/test_api_scrape_submission.py tests/unit/test_api_serializers.py tests/integration/test_routes_validation.py tests/integration/test_admin_read_pagination.py tests/integration/test_run_model_api.py -v`
- `poetry run ruff check src tests`

Definition of done for Workstream 3:
- `routes.py` loses repeated 400/404/202 shaping patterns.
- Pagination logic is implemented once.

---

## Workstream 4: Deduplicate selector strict vs forgiving paths

Priority: Medium
Risk: Medium
Payoff: Medium
Files:
- `src/scrapeyard/engine/selectors.py`
- `src/scrapeyard/engine/scraper.py`
- selector tests

### Objective
Eliminate lockstep duplication in selector extraction while preserving both strict and forgiving behavior.

### Task 4.1: Introduce one internal selector execution engine
Candidate shape:
- `_extract_selectors_impl(..., suppress_failures: bool)`
- `_select_items_impl(..., suppress_failures: bool)`
- `_count_selector_matches_impl(..., suppress_failures: bool)`

Notes:
- Public API can stay the same.
- Strict wrappers should raise `SelectorExecutionError`.
- Forgiving wrappers should log and return empty/None exactly as they do now.

### Task 4.2: Keep logging semantics stable
Notes:
- Preserve current debug logging for suppressed selector failures.
- Avoid double-logging when forgiving wrappers call the impl.

Verification:
- `poetry run pytest tests/unit/test_selectors.py tests/unit/test_scraper_decomposition.py tests/unit/test_scraper_failure_observability.py -v`

Definition of done for Workstream 4:
- One implementation path drives both strict and forgiving selector flows.

---

## Workstream 5: Simplify detection logic and trim non-essential commentary

Priority: Medium
Risk: Low to Medium
Payoff: Medium
Files:
- `src/scrapeyard/engine/detection.py`
- `tests/unit/test_detection.py`

### Objective
Make the branch-heaviest module easier to read without changing classification behavior.

### Task 5.1: Consolidate repeated text-normalization helpers
Candidates:
- unify `_normalize_price_text()` and `_normalize_stock_signal_text()` behind a shared helper for string/list-to-text normalization
- keep name-specific wrappers only if they materially aid readability

### Task 5.2: Reduce procedural state in pricing classification
Plan:
- Extract pattern-phase helpers such as:
  - `_match_call_for_price(...)`
  - `_match_display_text_pattern(...)`
  - `_match_css_display_text(...)`
  - `_match_price_value_pattern(...)`
- Return small tuples/objects instead of mutating `matched` and `display_text` through a long function.

### Task 5.3: Remove comments that just narrate the code
Targets:
- section banners and “Step 1 / Step 2 / Step 3” comments
- comments that simply restate what the next branch already says

Verification:
- `poetry run pytest tests/unit/test_detection.py -v`
- `poetry run ruff check src tests`

Definition of done for Workstream 5:
- `detection.py` is shorter, less comment-heavy, and easier to scan.

---

## Workstream 6: Reduce storage-layer repetition

Priority: Medium
Risk: Medium
Payoff: Medium
Files:
- `src/scrapeyard/storage/job_store.py`
- `src/scrapeyard/storage/result_store.py`
- storage tests

### Objective
Trim repeated DB/write patterns and isolate brittle error handling.

### Task 6.1: Consolidate result deletion behavior
Plan:
- Make `delete_results()` reuse the same delete-and-remove-files helper path as retention cleanup, or factor a clearer common primitive.

### Task 6.2: Reduce repeated write/update ceremony in `job_store.py`
Plan:
- Consider small helpers for:
  - execute-and-commit with missing-row enforcement
  - update statements that only differ in SQL/params
- Do not over-abstract SQL into unreadable generic builders.

### Task 6.3: Isolate duplicate-job uniqueness handling
Plan:
- Move the `IntegrityError` inspection into a dedicated helper so `save_job()` reads clearly.
- If SQLite makes cleaner detection impossible, at least confine the string-matching to one function.

Verification:
- `poetry run pytest tests/unit/test_job_store.py tests/unit/test_job_store_runs.py tests/unit/test_storage_job_queries.py tests/unit/test_result_store.py tests/unit/test_result_store_cleanup.py tests/unit/test_error_store.py -v`

Definition of done for Workstream 6:
- Deletion and update code paths are less repetitive.
- Uniqueness handling is isolated and easier to audit.

---

## Workstream 7: Remove unused-looking runtime ceremony

Priority: Low
Risk: Low
Payoff: Low to Medium
Files:
- `src/scrapeyard/main.py`
- `src/scrapeyard/api/dependencies.py`
- tests around startup/lifespan

### Objective
Trim app-state assignment that does not appear to be read anywhere meaningful.

Tasks:
1. Confirm via search/tests which `app.state` fields are actually needed.
2. Remove unused state assignments from `_assign_runtime_services()`.
3. If app state is intended for future access, document that explicitly; otherwise keep the startup code lean.

Verification:
- `poetry run pytest tests/unit/test_main.py tests/unit/test_dependencies.py tests/unit/test_runtime_health.py -v`

---

## Workstream 8: Final comment cleanup pass

Priority: Low
Risk: Low
Payoff: Low
Files:
- `src/scrapeyard/storage/database.py`
- `src/scrapeyard/common/settings.py`
- `src/scrapeyard/webhook/dispatcher.py`
- any module touched above

### Objective
Remove comments that add visual noise without adding lasting value.

Keep comments that explain:
- invariants
- crash-safety decisions
- non-obvious library quirks
- intentionally strange logic

Trim comments that only:
- restate field groupings
- narrate obvious control flow
- label compatibility hacks that no longer exist

Verification:
- `poetry run ruff check src tests`

---

## Recommended execution order

1. Workstream 1: worker-path decomposition
2. Workstream 2: alias cleanup
3. Workstream 3: API normalization
4. Workstream 4: selector deduplication
5. Workstream 5: detection simplification
6. Workstream 6: storage cleanup
7. Workstream 7: runtime ceremony cleanup
8. Workstream 8: final comment pass

Reasoning:
- 1 and 2 attack the biggest maintainability hotspot first.
- 3 and 4 remove obvious duplication next.
- 5 and 6 are worthwhile but slightly more stylistic/internal.
- 7 and 8 are cleanup polish after core refactors settle.

---

## Suggested slice sizing

If you want this executed safely, split it into these PR-sized slices:

- Slice A: Workstream 1 only
- Slice B: Workstream 2 only
- Slice C: Workstream 3 only
- Slice D: Workstream 4 + 5 together if small enough, otherwise separate
- Slice E: Workstream 6 only
- Slice F: Workstream 7 + 8

---

## Exit criteria for the whole cleanup plan

The cleanup is done when:
- hotspot files are materially shorter or simpler
- repeated response/selector/storage patterns are centralized
- alias-based test seams are removed or explicitly justified
- comment noise is reduced without harming clarity
- `poetry run ruff check src tests` passes
- `poetry run pytest` passes

---

## Short version

If we only do three things, do these:
1. break up `scrape_task()` / `apply_validation()` / `scrape_target()`
2. remove alias-based test seams
3. centralize API response/error/pagination plumbing
