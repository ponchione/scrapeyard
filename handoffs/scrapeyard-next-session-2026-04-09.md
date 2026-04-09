# Scrapeyard Next Session Handoff — 2026-04-09

## Completed this session

Resolved TECH-DEBT slices A-H.

### Slice A: Worker orchestration decomposition
- Split `src/scrapeyard/queue/worker.py` into focused helper modules:
  - `src/scrapeyard/queue/run_lifecycle.py`
  - `src/scrapeyard/queue/validation_policy.py`
  - `src/scrapeyard/queue/target_execution.py`
  - `src/scrapeyard/queue/job_state.py`
  - `src/scrapeyard/queue/error_records.py`
- Kept `worker.py` as the stable orchestration/patch surface with compatibility aliases.
- `worker.py` is now orchestration-first instead of carrying most lifecycle logic inline.

### Slice B: Scraper engine separation of concerns
- Split `src/scrapeyard/engine/scraper.py` into focused helper modules:
  - `src/scrapeyard/engine/scrape_models.py`
  - `src/scrapeyard/engine/browser_debug.py`
  - `src/scrapeyard/engine/fetch_classifier.py`
  - `src/scrapeyard/engine/pagination.py`
  - `src/scrapeyard/engine/adaptive_diagnostics.py`
- Kept `scraper.py` as the stable import/patch surface with compatibility aliases.
- `scraper.py` is now much smaller and more testable while preserving behavior.

### Slice C: Route/controller thinning and response shaping cleanup
- Added:
  - `src/scrapeyard/api/serializers.py`
  - `src/scrapeyard/api/scrape_submission.py`
  - `src/scrapeyard/api/response_utils.py`
- Moved payload shaping for jobs/runs/errors/results into serializer helpers.
- Moved ad-hoc scrape submission policy and sync-vs-async wait behavior into `scrape_submission.py`.

### Slice D: Failure-mode visibility and exception-swallowing hardening
- Added strict selector execution helpers in `src/scrapeyard/engine/selectors.py` plus a structured `SelectorExecutionError` surface.
- `src/scrapeyard/engine/scraper.py` now fails targets with `selector_engine_error` instead of silently collapsing selector-engine exceptions into empty results.
- Browser debug capture fallbacks in `src/scrapeyard/engine/browser_debug.py` now log capture failures without crashing runs.

### Slice E: Domain modeling consistency cleanup
- Added typed `TargetStatus` handling in `src/scrapeyard/engine/scrape_models.py`.
- Updated scraper, validation-policy, and worker flows to use typed target-status checks instead of raw string-literal comparisons in core lifecycle logic.
- Kept external payloads stable by serializing target statuses back to strings.

### Slice F: Health/runtime wiring separation
- Moved health/project-summary aggregation into `src/scrapeyard/runtime/health.py`.
- Added `RuntimeServices`, `build_runtime_services()`, and `reset_cached_dependencies()` in `src/scrapeyard/api/dependencies.py`.
- Slimmed `main.py` lifespan orchestration with startup/shutdown helpers that operate on materialized runtime services.

### Slice G: Storage-layer growth management
- Split job-row mapping into `src/scrapeyard/storage/job_rows.py` and shared job SQL constants into `src/scrapeyard/storage/job_sql.py`.
- Moved reporting/query-shaping helpers into:
  - `src/scrapeyard/storage/job_queries.py`
  - `src/scrapeyard/storage/error_queries.py`
  - `src/scrapeyard/storage/result_queries.py`
- Kept concrete stores focused on DB orchestration and protocol semantics.

### Slice H: Consistency and polish cleanup
- Added `src/scrapeyard/common/time.py` and switched touched modules to `utc_now()`.
- Moved Linux RSS parsing into `src/scrapeyard/queue/memory.py` so `queue/pool.py` no longer carries `/proc/self/statm` handling inline.
- Added `src/scrapeyard/api/query_parsing.py` and `no_content_response()` to further reduce route-level boilerplate.

## TECH-DEBT status
- `TECH-DEBT.md` now marks slices A-H resolved.
- Active slices: none.
- Ranked active debt list is empty.

## Verification completed

Targeted checks run successfully:
- `poetry run pytest --no-cov tests/unit/test_storage_job_queries.py tests/unit/test_storage_error_queries.py tests/unit/test_job_store.py tests/unit/test_job_store_runs.py tests/unit/test_error_store.py tests/unit/test_result_store_cleanup.py`
- `poetry run pytest --no-cov tests/unit/test_common_time.py tests/unit/test_queue_memory.py tests/unit/test_api_query_parsing.py tests/unit/test_pool.py tests/unit/test_api_scrape_submission.py tests/unit/test_job_store.py tests/unit/test_job_store_runs.py tests/unit/test_result_store_cleanup.py tests/unit/test_error_store.py tests/unit/test_runtime_health.py tests/integration/test_routes_validation.py tests/integration/test_run_model_api.py`

Repo-wide verification run successfully:
- `poetry run ruff check src tests`
- `poetry run pytest`
  - Result at handoff time: `529 passed, 3 skipped`
  - Coverage: `92.51%`

## Runtime status
- Docker container was rebuilt and restarted with `docker compose up -d --build scrapeyard`.
- `docker compose ps` shows the `scrapeyard` service up on port `8420`.
- `curl http://127.0.0.1:8420/health` returned `status: ok` after restart.

## Documentation cleanup completed
- `README.md` now reflects JSON-only result artifacts instead of the removed multi-format output modes.
- `CHANGELOG.md` now includes a `0.5.1` entry covering the debt-slice cleanup pass.
- This handoff was refreshed so it no longer points to now-completed slices G/H.

## Notes for next agent
- `worker.py` and `scraper.py` remain the stable patch surfaces for future extraction work because tests still monkeypatch there.
- No active TECH-DEBT slices remain; future work should be feature-driven or based on a fresh audit.
- Do not push unless explicitly asked.
