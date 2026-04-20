Start with Slice B from `CONCRETE_CLEANUP_PLAN.md` in this Scrapeyard repo.

Your job is to execute only Workstream 2: remove alias-based compatibility clutter. Do not mix in other cleanup workstreams unless you discover a tiny prerequisite change that is strictly required to complete Slice B safely.

Read these files first, in this order:
1. `AGENTS.md`
2. `CONCRETE_CLEANUP_PLAN.md` (focus on Workstream 2)
3. `WIDE_AUDIT_FINDINGS.md` (focus on the alias-compatibility smell)
4. `src/scrapeyard/queue/worker.py`
5. `src/scrapeyard/engine/scraper.py`
6. The most relevant existing tests:
   - `tests/unit/test_worker_decomposition.py`
   - `tests/unit/test_worker_error_handling.py`
   - `tests/unit/test_scraper_decomposition.py`
   - `tests/unit/test_scraper_failure_observability.py`
   - `tests/unit/test_scraper_pagination.py`

Objective:
Stop carrying dead-feeling production aliases solely for test patching. Update tests to use the real seam they actually depend on, then delete the alias blocks from `worker.py` and `scraper.py`.

Current repo context from Slice A:
- Workstream 1 is already done. `worker.py`, `validation_policy.py`, and `scraper.py` were decomposed into smaller helpers and the full test suite passed afterward.
- The remaining alias blocks are still present at the bottom of:
  - `src/scrapeyard/queue/worker.py`
  - `src/scrapeyard/engine/scraper.py`
- Do not undo or reshape the Slice A helper extractions unless a tiny rename/import adjustment is required for Slice B.

Known likely alias consumers to verify before editing:
- `tests/unit/test_worker_decomposition.py`
  - currently imports `_resolve_target_runtime_context` from `scrapeyard.queue.worker`
  - likely should import `resolve_target_runtime_context` from `scrapeyard.queue.target_execution`
- `tests/unit/test_worker_error_handling.py`
  - currently imports `_finalize_run` from `scrapeyard.queue.worker`
  - likely should import `finalize_run` from `scrapeyard.queue.run_lifecycle`
- `tests/unit/test_scraper_decomposition.py`
  - currently imports `_browser_fetch_kwargs`, `_default_debug_blob`, `_missing_adaptive_selectors`, `_response_title` from `scrapeyard.engine.scraper`
  - likely should import them from their owning modules:
    - `scrapeyard.engine.browser_debug`
    - `scrapeyard.engine.adaptive_diagnostics`
- `tests/unit/test_scraper_pagination.py`
  - currently imports `_resolve_href` from `scrapeyard.engine.scraper`
  - likely should import `resolve_href` from `scrapeyard.engine.pagination`
- `tests/unit/test_scraper_failure_observability.py`
  - currently monkeypatches `scrapeyard.engine.scraper._fetch_page`
  - this is a real helper, not part of the alias block; do not remove that seam unless you prove a better direct seam and keep behavior identical

Scope boundaries:
- In scope:
  - `src/scrapeyard/queue/worker.py`
  - `src/scrapeyard/engine/scraper.py`
  - directly affected unit tests only
- Out of scope:
  - further worker/scraper decomposition
  - API cleanup
  - selector strict/non-strict deduplication
  - storage/runtime cleanup
  - broad rename churn unrelated to alias removal

Required execution order:
1. Build a concrete mapping of test file -> alias dependency before editing.
2. Update tests to import/patch the real helper names or stable module seams.
3. Run the affected tests and confirm they pass before deleting alias blocks.
4. Delete the alias blocks from `worker.py` and `scraper.py`.
5. Re-run verification and report exactly what changed.

Non-negotiables:
- Preserve existing runtime behavior.
- Keep the current package layout.
- Prefer patching/importing the actual symbol used by the code under test.
- If a seam is truly needed, introduce one explicit seam instead of keeping many bottom-of-file aliases.
- Do not widen the change into a larger refactor just because some tests are awkward.
- Keep `_fetch_page` patchability intact unless you replace it with a clearly better direct seam and update all dependent tests in the same slice.

What “done” looks like for Slice B:
- No bottom-of-file compatibility alias block remains in `worker.py`.
- No bottom-of-file compatibility alias block remains in `scraper.py`.
- Affected tests import/patch supported seams directly.
- `ruff` passes.
- Relevant unit tests pass.

Verification commands you must run:
- `poetry run pytest tests/unit/test_worker_decomposition.py tests/unit/test_worker_error_handling.py tests/unit/test_scraper_decomposition.py tests/unit/test_scraper_failure_observability.py tests/unit/test_scraper_pagination.py -v --no-cov`
- `poetry run ruff check src tests`

Recommended extra verification if your edits touch helper ownership more broadly:
- `poetry run pytest tests/unit/test_validation_policy.py tests/unit/test_scraper_pagination_helpers.py -v --no-cov`
- `poetry run pytest`

If the affected tests reveal an unrelated failure:
- do not sprawl into a broad cleanup
- isolate whether it blocks Slice B
- fix it only if it is a direct prerequisite
- otherwise note it explicitly and stop

Deliverables for this session:
1. The code changes for Slice B
2. Any updated tests/import paths/patch seams
3. A concise mapping of which tests depended on which aliases
4. Verification results with exact commands run
5. Any risks or follow-up items that belong in Slice C rather than now
