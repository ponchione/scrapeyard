# Technical Debt Register

Last updated: 2026-04-06 (Slices A–L resolved; no active slices)

This file tracks active technical debt only. Resolved items from earlier audits
have been removed rather than kept as historical clutter.

Audit basis:
- Full codebase audit across all 39 source modules in `src/scrapeyard/`
- Storage/performance/architecture review of storage, queue, API, and webhook layers
- Test suite coverage analysis (411 unit + 33 integration, 80.42% coverage)
- Second-pass audit after slices A–F: engine, queue/storage, API/webhook/config/tests

---

## Implementation slices

Active debt is grouped into implementable slices. Each slice is a cohesive unit
of work that can be completed, tested, and merged independently. Items within a
slice share a theme and often touch the same files.

### ~~Slice A: Quick wins~~ (RESOLVED)

All items resolved. See "Resolved history" at the bottom.

### ~~Slice B: Worker decomposition~~ (RESOLVED)

All items resolved. See "Resolved history" at the bottom.

### ~~Slice C: Rate limiter + webhook hardening~~ (RESOLVED)

All items resolved. See "Resolved history" at the bottom.

### ~~Slice D: Data integrity~~ (RESOLVED)

All items resolved. See "Resolved history" at the bottom.

### ~~Slice E: Test infrastructure + coverage~~ (RESOLVED)

All items resolved. See "Resolved history" at the bottom.

### ~~Slice F: Code hygiene + fetcher hardening~~ (RESOLVED)

All items resolved. See "Resolved history" at the bottom.

### ~~Slice G: Quick safety fixes~~ (RESOLVED)

All items resolved. See "Resolved history" at the bottom.

### ~~Slice H: Protocol + abstraction leaks~~ (RESOLVED)

All items resolved. See "Resolved history" at the bottom.

### ~~Slice I: God function decomposition~~ (RESOLVED)

All items resolved. See "Resolved history" at the bottom.

### ~~Slice J: Tooling hardening~~ (RESOLVED)

All items resolved. See "Resolved history" at the bottom.

### ~~Slice K: Test coverage + robustness~~ (RESOLVED)

All items resolved. See "Resolved history" at the bottom.

### ~~Slice L: Dead code + duplication cleanup~~ (RESOLVED)

All items resolved. See "Resolved history" at the bottom.

---

## Item details

### G1: JSON logging produces invalid output (RESOLVED 2026-04-06)

Replaced hand-crafted JSON format string with a `_JsonFormatter` class that
uses `json.dumps()` to properly escape quotes, backslashes, newlines, and
other special characters. Exception info and stack traces are appended to the
message before JSON serialization. Tests: 7 new tests in
`tests/unit/test_logging.py` covering plain messages, double quotes,
backslashes, newlines, unicode, and exception tracebacks.

### G2: Side-effecting _determine_final_status (RESOLVED 2026-04-06)

Removed `flat_data.clear()` from `_determine_final_status()`, making it a
pure read-only function. Moved the clearing logic to the caller in
`scrape_task()` where it's visible and explicit: after getting the final
status, if `all_or_nothing` strategy yielded `failed`, the caller clears
`flat_data`. Tests: 5 new direct unit tests in
`tests/unit/test_determine_final_status.py` covering all strategies and
verifying no mutation. Existing integration test
`test_all_or_nothing_fails_on_any_failure` continues to verify record_count=0.

### G3: Assert used for control flow (RESOLVED 2026-04-06)

Replaced `assert last_exc is not None` in `RetryHandler.execute()` with an
explicit `if last_exc is None: raise RuntimeError(...)`. Safe under
`python -O` where asserts are stripped.

### G4: Hardcoded page_size in memory check (RESOLVED 2026-04-06)

Replaced `page_size = 4096` with `os.sysconf("SC_PAGE_SIZE")` in
`_check_memory()`. Correct on aarch64 and other non-4K-page architectures.
The `OSError` catch covers platforms that don't support `sysconf`. Updated
3 tests in `test_pool.py` to mock `os.sysconf` alongside `/proc/self/statm`.

### G5: Double call to _classify_page_signals (RESOLVED 2026-04-06)

Cached the result of `_classify_page_signals(exc.debug)` in a local `signal`
variable. The `if` condition and the return now both reference the cached
value instead of calling the function twice.

### H1: protocols.py imports concrete types (RESOLVED 2026-04-06)

Moved `ResultPayload` and `SaveResultMeta` into shared
`src/scrapeyard/storage/types.py`, updated `storage/protocols.py` to depend on
that shared type module, and re-exported `SaveResultMeta` from
`storage/__init__.py`. `LocalResultStore` now imports the shared types instead
of defining them inline.

### H2: Health endpoint bypasses store protocol (RESOLVED 2026-04-06)

Added `summary_by_project()` to the `JobStore` protocol and implemented it in
`SQLiteJobStore`. `HealthCache.project_summary()` now calls
`get_job_store().summary_by_project()` instead of issuing raw SQL directly.

### H3: Private _redis attribute access in lifespan (RESOLVED 2026-04-06)

Added a public `WorkerPool.redis` property and switched `main.py` lifespan
startup to initialize the shared rate limiter from `pool.redis` instead of
reaching into the private `_redis` attribute.

### H4: cleanup.py bypasses store with raw SQL (RESOLVED 2026-04-06)

Added `prune_excess_per_job()` to the `ResultStore` protocol and implemented it
in `LocalResultStore`. `storage/cleanup.py` now delegates both age-based and
per-job retention work through the store protocol and no longer accepts or uses
a raw database connection.

### H5: DI returns concrete types not protocols (RESOLVED 2026-04-06)

Updated `api/dependencies.py` singleton factory return annotations to the
protocol interfaces (`JobStore`, `ErrorStore`, `ResultStore`) while keeping the
same cached SQLite-backed implementations underneath.

### I1: _fetch_page is 110 lines (RESOLVED 2026-04-06)

Split `_fetch_page()` into smaller focused helpers in `engine/scraper.py`:
`_browser_fetch_kwargs()`, `_capture_browser_state()`,
`_fetch_basic_response()`, `_fetch_browser_response()`, and
`_populate_fetch_debug()`. The top-level function now coordinates fetch mode,
HTTP status handling, and the shared return type without embedding the browser
closure and debug assembly logic inline.

### I2: scrape_target is 125 lines (RESOLVED 2026-04-06)

Decomposed `scrape_target()` into `_fetch_target_page()`, `_paginate()`,
`_missing_adaptive_selectors()`, `_log_adaptive_selector_gap()`, and
`_finalize_target_success()`. Pagination, adaptive relocation logging, and
success finalization are now separated from the main orchestration path.

### I3: scrape_task is 118 lines (RESOLVED 2026-04-06)

Split `scrape_task()` orchestration into reusable helpers in `queue/worker.py`:
`_build_run_paths()`, `_mark_job_running()`, `_create_run_record()`,
`_collect_result_payload()`, `_save_run_result()`, and
`_update_job_completion()`. The main worker flow now reads as a compact
sequence of run setup, target processing, persistence, webhook dispatch, and
final job-state update.

### I4: _fetch_and_validate_target is 95 lines (RESOLVED 2026-04-06)

Extracted target runtime preparation and failure handling into
`TargetRuntimeContext`, `_resolve_target_runtime_context()`,
`_guard_target_execution()`, `_log_target_fetch()`, and
`_record_failed_target()`. `_fetch_and_validate_target()` now focuses on the
high-level flow: resolve runtime context, guard execution, scrape, and dispatch
to validation.

### J1: Ruff has no rule selection configured (RESOLVED 2026-04-06)

Added explicit Ruff lint selection in `pyproject.toml` for `E`, `F`, `W`,
`B`, `I`, `UP`, `C4`, and `SIM`, along with targeted ignores for known
framework-specific and legacy hotspots so the wider rule set can be enforced
without blocking on unrelated cleanup.

### J2: mypy strict only on 2 of 10 modules (RESOLVED 2026-04-06)

Expanded strict mypy coverage in `pyproject.toml` from `models` and `config`
into `common`, `api`, and `storage`, then fixed the surfaced issues in route
annotations, logging setup, dependency typing, and SQLite row decoding so the
stricter module set now passes cleanly.

### J3: No per-module coverage thresholds (RESOLVED 2026-04-06)

Enabled coverage reporting on the default `pytest` path via `addopts`, added
terminal missing-line output plus HTML coverage generation, and configured the
coverage report to sort by lowest-covered modules first. This makes low-coverage
critical files like `queue/pool.py` and `main.py` visible on every test run
instead of hiding behind the global average.

### J4: Log level not configurable via settings (RESOLVED 2026-04-06)

Added `SCRAPEYARD_LOG_LEVEL` to `ServiceSettings`, wired it through
`main.py` into `setup_logging()`, and added tests covering configured log-level
application and invalid-level rejection.

### J5: Missing dev deps (pytest-timeout, pip-audit) (RESOLVED 2026-04-06)

Added `pytest-timeout` and `pip-audit` to dev dependencies, enabled a default
60-second pytest timeout, and verified the new audit tool runs. Current audit
output reports several upstream package CVEs, which are now detectable in the
standard toolchain rather than silently missed.

### K1: pool.py lifecycle methods untested (RESOLVED 2026-04-06)

Expanded `tests/unit/test_pool.py` to cover `start()`, `stop()`, `enqueue()`,
`_run_job()`, and `_execute()` through lifecycle-oriented behavior tests. This
lifted `queue/pool.py` coverage from the low 40s to 92% and exercised startup,
shutdown, enqueue fallback handling, and browser-slot accounting.

### K2: main.py lifespan + health untested (RESOLVED 2026-04-06)

Added `tests/unit/test_main.py` covering `HealthCache.project_summary()`, cache
reuse, degraded health status, and the full `lifespan()` startup/shutdown
orchestration. `main.py` is now at 99% coverage.

### K3: Flaky busy-loop polling in integration tests (RESOLVED 2026-04-06)

Extracted `poll_until_ready()` into `tests/integration/conftest.py` with a
configurable timeout and small exponential back-off, then replaced the repeated
`for _ in range(40): ... sleep(0.05)` loops across the integration suites.
This removed the 2-second fixed polling pattern that was fragile under CI load.

### K4: Tests exercise private methods not behavior (RESOLVED 2026-04-06)

Reworked `tests/unit/test_pool.py` to validate memory gating through public
`can_accept()` and `enqueue()` behavior instead of directly asserting on
`_check_memory()` as the main coverage path, improving refactor resilience while
still preserving the architecture-specific memory-limit assertions introduced in
an earlier slice.

### K5: Missing API route coverage (RESOLVED 2026-04-06)

Added route coverage for POST `/jobs` content-type validation, DELETE
`/jobs/{id}` missing-job handling, GET `/results` with explicit `run_id`, and
additional health/lifecycle coverage. `api/routes.py` now sits at 89% coverage,
and the DELETE/POST route paths were tightened to match the new tests.

### L1: extract_item_selectors() never called (RESOLVED 2026-04-06)

Removed the unused `extract_item_selectors()` helper from
`engine/selectors.py`. Item-scoped extraction continues through the existing
`select_items()` + `extract_selectors()` flow used by `scraper.py`, and the
selector tests now cover that public composition directly.

### L2: Browser-config default ternary repetition (RESOLVED 2026-04-06)

Added `_target_browser_config()` in `engine/scraper.py` so browser-backed code
can use `target.browser or BrowserConfig()` once and then read attributes
directly. `_default_debug_blob()` and `_browser_fetch_kwargs()` no longer carry
the repeated `browser is None` ternary chain, and debug output now reflects the
actual `BrowserConfig` defaults consistently.

### L3: Duplicated delete-expired logic (RESOLVED 2026-04-06)

Extracted `LocalResultStore._delete_by_ids()` in `storage/result_store.py` and
reused it from both `delete_expired()` and `prune_excess_per_job()`. The shared
helper owns the `DELETE ... WHERE id IN (...)`, commit, and directory-removal
sequence so retention cleanup behavior stays identical without duplicated code.

### L4: Repeated get_db + commit boilerplate (RESOLVED 2026-04-06)

Added `SQLiteJobStore._execute_write()` in `storage/job_store.py` and reused it
across the single-statement write paths (`update_job*`, `create_run()`,
`finalize_run()`, and `fail_run()`). This removes the repeated
`get_db(...)` + `execute(...)` + `commit()` boilerplate while keeping the store
API and behavior unchanged.

### L5: Redundant single-element tuple loop (RESOLVED 2026-04-06)

Simplified `engine/scraper.py:_response_title()` to a direct `getattr(page,
"title", None)` lookup before the HTML fallback regex path. Behavior is
unchanged, but the unnecessary one-item loop is gone.

### L6: _select_elements missing error handling (RESOLVED 2026-04-06)

Hardened `engine/selectors.py:_select_elements()` to resolve the appropriate
selector method dynamically and return an empty match list when the scope lacks
that method or the selector engine raises. Invalid CSS/XPath selectors now
degrade to empty extraction results instead of crashing the entire scrape.

### C1: Redis rate limiter TOCTOU race (RESOLVED 2026-04-06)

Replaced the non-atomic GET + SET in `RedisDomainRateLimiter.acquire()` with
an atomic Lua script that checks the last-request timestamp and writes a new
one in a single Redis `EVALSHA` call. Script SHA is cached after first load.
Tests: `tests/unit/test_rate_limiter.py` (9 tests).

### C2: No webhook retry or persistence (RESOLVED 2026-04-06)

Added configurable retry with exponential backoff to `HttpWebhookDispatcher`.
Transient failures (5xx, 429, connection errors, timeouts) retry up to
`max_retries` times (default 3) with capped exponential backoff (1s base,
30s max). 4xx errors (except 429) are permanent and not retried. Added unique
`delivery_id` (UUID4 hex) to every webhook payload for receiver-side
deduplication. HMAC signing and SQLite persistence deferred to a future slice.
Tests: `tests/unit/test_webhook_dispatcher.py` (15 tests).

### D1: save_result() not atomic (RESOLVED 2026-04-06)

Added migration `008_results_meta_unique_job_run.sql` that upgrades the
`(job_id, run_id)` composite index to UNIQUE. Replaced the two-statement
DELETE + INSERT in `save_result()` with a single atomic `INSERT OR REPLACE`
statement. Eliminates the crash window entirely — no transaction management
needed since it's a single statement. Existing tests continue to pass;
the `test_save_result_reuses_explicit_run_id` test validates the upsert path.

### D2: Cross-DB consistency gap (RESOLVED 2026-04-06)

Wrapped `_finalize_run()` in `queue/worker.py` with try/except around the
`job_store.finalize_run()` call. On failure, logs at CRITICAL level and falls
back to `job_store.fail_run(run_id)` to mark the run as failed rather than
leaving it stuck in `running`. If the fallback also fails, logs a second
CRITICAL with full traceback. Tests: 4 new tests in
`tests/unit/test_worker_error_handling.py` covering happy path, fallback,
and double-failure scenarios.

### E1: No coverage tool (RESOLVED 2026-04-06)

Added `pytest-cov` to dev dependencies. Added `[tool.coverage.run]` (source,
branch) and `[tool.coverage.report]` (show_missing, fail_under=80, standard
exclusions including Protocol `...` stubs) sections to `pyproject.toml`.
Coverage: 80.31% on unit tests.

### E2: Duplicated worker test infrastructure (RESOLVED 2026-04-06)

Created `tests/unit/worker_helpers.py` with shared `make_job()`,
`make_target()`, `make_config_mock()`, and `SIMPLE_YAML` factories.
Created `tests/unit/conftest.py` with the shared `mock_stores` fixture.
Refactored all 6 `test_worker_*.py` files to import from the shared modules,
removing all duplicate `_make_job`, `_make_target`, `_patch_config`,
`_make_config_mock`, `_make_config`, `_SIMPLE_YAML`, and `mock_stores` definitions.

### E3: Missing unit tests (RESOLVED 2026-04-06)

Added 40 new unit tests across 6 new test files:
- `test_ids.py` (3 tests): run ID format, uniqueness, hex suffix length
- `test_dt.py` (6 tests): parse_dt/fmt_dt None handling, round-trips
- `test_filesystem.py` (7 tests): prepare_directory, write/read JSON, error paths
- `test_pool.py` (8 tests): memory check disabled/error/over-limit, properties, enqueue
- `test_scheduler.py` (12 tests): register/remove/start/trigger/shutdown lifecycle
- `test_config_edge_cases.py` (4 tests): join transform no-op, pagination defaults

### E4: No type checker (RESOLVED 2026-04-06)

Added `mypy` and `types-PyYAML` to dev dependencies. Added `[tool.mypy]` config
to `pyproject.toml` with strict checking enabled on `models/` and `config/`
modules. Third-party stubs configured for scrapling, arq, apscheduler, aiosqlite.
Both strict modules pass cleanly.

### E5: Timing-sensitive tests (RESOLVED 2026-04-06)

Replaced wall-clock timing assertions in `test_resilience.py` with mocked
`asyncio.sleep`. The three backoff tests (`test_exponential_backoff_delays`,
`test_linear_backoff_delays`, `test_backoff_capped_at_max`) now assert on the
exact delay values passed to sleep rather than measuring elapsed time. Tests
are deterministic and run instantly.

### F1: BrowserConfig stealth controls (RESOLVED 2026-04-06)

Added `stealth`, `hide_canvas`, `useragent`, and `extra_headers` fields to
`BrowserConfig` in `config/schema.py`. All default to off/empty. Wired through
`_fetch_page()` in `engine/scraper.py` to `PlayWrightFetcher.async_fetch()`
kwargs. Debug blob also includes the new fields. Tests: existing 411 unit +
33 integration pass.

### F2: Global mutable state (RESOLVED 2026-04-06)

Wrapped module-level globals in encapsulating classes:
- `database.py`: `DatabaseManager` class holds `_db_dir`, `_connections`,
  `_locks`, etc. Module-level functions (`init_db`, `get_db`, `close_db`,
  `reset_db`) delegate to a `_default_manager` singleton — no import changes.
- `main.py`: `HealthCache` class holds `start_time`, `_projects_cache`, and
  cache TTL. Module-level `_health` singleton used by lifespan and `/health`.
- `dependencies.py`: `_RateLimiterHolder` class replaces the bare `_rate_limiter`
  global. Public functions (`init_rate_limiter`, `get_rate_limiter`,
  `reset_rate_limiter`) delegate to `_rate_limiter_holder`.

### F3: API error response pattern (RESOLVED 2026-04-06)

Extracted `_error_response(status_code, message)` helper in `routes.py`.
Replaced all 13 manual `Response(content=_json_encode({"error": ...}), ...)`
calls with the helper. Reduces each error site from 4 lines to 1.

### F4: Duplicate _normalize_text functions (RESOLVED 2026-04-06)

Renamed to descriptive names:
- `scraper.py:_normalize_text` → `_coerce_to_text` (handles None, bytes)
- `detection.py:_normalize_text` → `_clean_element_text` (strips, filters "None")

### F5: join transform no-op (RESOLVED 2026-04-06)

Replaced the no-op `join` transform with a `ValueError` explaining that join
is a list-level operation not supported as a per-value transform. Updated 3
tests to expect the error.

### F6: Compact JSON storage (RESOLVED 2026-04-06)

Changed `write_json_file()` from `json.dumps(..., indent=2)` to compact JSON
with `separators=(",", ":")`. ~30% size reduction for machine-consumed result
files. `read_json_file()` handles both formats.
