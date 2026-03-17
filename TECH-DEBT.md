# Technical Debt Register

Last updated: 2026-03-17

This document supersedes the deleted `docs/TECH-DEBT.md`.

Audit basis:
- Manual review of the current `src/` tree and selected tests
- `poetry run ruff check src tests` on 2026-03-17
- Existing integration and unit tests in `tests/`

## TD-001: Queue backend is still in-memory and non-durable

Severity: medium

Summary:
The project still uses a custom in-memory queue instead of the `arq`-based path described by the spec and kept as a dependency in `pyproject.toml`.

Evidence:
- `src/scrapeyard/queue/pool.py`
- `src/scrapeyard/api/dependencies.py`
- `pyproject.toml`

Impact:
- Queued jobs are lost on process restart.
- The implementation now has real queue-semantics debt of its own, which increases the eventual migration cost.

Recommendation:
- Either migrate to a durable queue backend, or explicitly ratify the in-memory queue as the intended architecture and remove stale `arq` expectations from the codebase and docs.

## TD-002: WorkerPool drains the queue into untracked tasks and does not shut down cleanly

Severity: high

Summary:
`WorkerPool._consume()` immediately converts queued items into background tasks via `asyncio.create_task`, and `stop()` only cancels the consumer loop. It does not track or await the spawned work.

Evidence:
- `src/scrapeyard/queue/pool.py:116-170`
- `src/scrapeyard/main.py:61-69`

Impact:
- In-flight jobs can continue running after shutdown begins.
- The app can close databases while scrape tasks are still active.
- The priority queue stops being real backpressure because queued items are rapidly converted into pending tasks waiting on semaphores.

Recommendation:
- Track spawned worker tasks and await or cancel them during shutdown.
- Prefer consuming under semaphore control instead of creating an unbounded backlog of pending tasks.

## TD-003: Scheduled jobs ignore execution priority and browser requirements

Severity: high

Summary:
When APScheduler fires a scheduled job, `_trigger_job()` always enqueues it with priority `"normal"` and never sets `needs_browser`.

Evidence:
- `src/scrapeyard/scheduler/cron.py:97-109`
- `src/scrapeyard/api/routes.py:128-137`
- `src/scrapeyard/queue/pool.py:151-162`

Impact:
- `execution.priority` on scheduled jobs is currently ignored.
- Scheduled `stealthy` and `dynamic` jobs bypass the browser semaphore, so browser concurrency limits are not enforced for scheduled work.

Recommendation:
- Load the job config in `_trigger_job()` and derive both `priority` and `needs_browser` exactly as the ad hoc route does.
- Add scheduler execution tests, not just create/list/delete tests.

Status: Resolved by reading each job’s config in `_trigger_job()` and passing the configured priority and browser flag to `WorkerPool.enqueue`. Verified by `tests/integration/test_scheduled_job_lifecycle.py::test_scheduler_respects_priority_and_browser`.

## TD-004: Async enqueue failures can leave a persisted job stranded and return a 500

Status: resolved (2026-03-17)

Severity: high

Summary:
`POST /scrape` saves the job record before calling `worker_pool.enqueue()`. If enqueue raises `MemoryError`, the route has no recovery path.

Evidence:
- `src/scrapeyard/api/routes.py:88-145`
- `src/scrapeyard/queue/pool.py:85-109`

Impact:
- The client gets a generic server error instead of a controlled capacity response.
- The job record is already saved, so the database can contain a permanently queued job that was never actually enqueued.

Recommendation:
- Catch `MemoryError` in the async route and return a `503`.
- Either delete the just-created job or mark it failed/rejected before responding.

Resolved by catching `MemoryError` in `POST /scrape`, deleting the just-created ad hoc job, and returning `503`. Verified by `tests/integration/test_routes_validation.py::test_async_scrape_enqueue_memory_error_returns_503_and_removes_job`.

## TD-005: Validation `on_empty` actions are defined but not implemented by the worker

Status: resolved (2026-03-17)

Severity: high

Summary:
`ValidationConfig.on_empty` supports `retry`, `warn`, `fail`, and `skip`, but the worker only looks at `validation.passed`. The returned `ValidationResult.action` is effectively ignored.

Evidence:
- `src/scrapeyard/engine/resilience.py:62-97`
- `src/scrapeyard/queue/worker.py:137-166`

Impact:
- Config values like `retry` and `skip` do not change runtime behavior.
- The schema advertises control that the worker does not actually honor.

Recommendation:
- Decide whether validation actions are part of the contract.
- If yes, implement the behavior in `scrape_task`.
- If not, remove or narrow the config surface to match reality.

Resolved by applying validation at the target level in `scrape_task`, with explicit `warn`, `skip`, `fail`, and `retry` handling plus focused unit coverage in `tests/unit/test_worker_validation_actions.py`.

## TD-006: `json+markdown` output does not generate real markdown

Status: resolved (2026-03-17)

Severity: high

Summary:
The formatter factory returns `format_json` for `json+markdown`, and `LocalResultStore.save_result()` writes the same formatted object to both `results.json` and `results.md`.

Evidence:
- `src/scrapeyard/formatters/factory.py:29-37`
- `src/scrapeyard/queue/worker.py:168-196`
- `src/scrapeyard/storage/result_store.py:70-76`
- `tests/unit/test_formatters.py:127-129`
- `tests/unit/test_result_store.py:48-58`

Impact:
- The `results.md` artifact for `json+markdown` is currently a stringified Python object, not markdown.
- Tests only assert file existence, so the bad artifact is not caught.

Recommendation:
- Generate JSON and markdown separately for `json+markdown`.
- Extend tests to assert artifact contents, not just filenames.

## TD-007: Logging setup is not idempotent

Status: resolved (2026-03-17)

Severity: medium

Summary:
`setup_logging()` adds new root handlers every time the app lifespan starts. There is no guard against duplicate handlers.

Evidence:
- `src/scrapeyard/common/logging.py:8-31`
- `src/scrapeyard/main.py:34-36`

Impact:
- Repeated app startups in tests, reloads, or embedded usage can duplicate log lines and leak file handlers.

Recommendation:
- Make logger setup idempotent by checking existing handlers, clearing managed handlers, or using a named logger tree instead of mutating the root logger repeatedly.

Resolved by making `setup_logging()` a one-time process-level initializer and adding idempotence coverage in `tests/unit/test_health.py::test_setup_logging_is_idempotent`.

## TD-008: Cleanup logic is duplicated and the `ResultStore` contract is drifting

Severity: medium

Summary:
`ResultStore.delete_expired()` exists on the protocol and in `LocalResultStore`, but production cleanup bypasses it and reimplements retention deletion directly against SQLite and the filesystem.

Evidence:
- `src/scrapeyard/storage/protocols.py:25-36`
- `src/scrapeyard/storage/result_store.py:143-166`
- `src/scrapeyard/storage/cleanup.py:21-89`

Impact:
- Retention logic now lives in two places.
- Interface-level cleanup behavior can drift from the actual cleanup loop.
- The abstraction is less useful because the production path does not use it.

Recommendation:
- Consolidate cleanup behavior behind one path.
- Either remove `delete_expired()` from the protocol or have the cleanup loop call the store implementation.

## TD-009: Relative pagination URL resolution is brittle

Status: resolved (2026-03-17)

Severity: medium

Summary:
Pagination link resolution is implemented manually instead of using `urllib.parse.urljoin`.

Evidence:
- `src/scrapeyard/engine/scraper.py:177-194`

Impact:
- Relative links can be resolved incorrectly for single-segment paths and other edge cases.
- The code is harder to trust than a standard-library resolver.

Recommendation:
- Replace `_resolve_href()` path concatenation with `urljoin(base_url, href)`.
- Add tests for single-segment, trailing-slash, query-string, and `../` cases.

Resolved by switching `_resolve_href()` to `urllib.parse.urljoin` and adding focused coverage in `tests/unit/test_scraper_pagination.py`.

## TD-010: Low-severity hygiene and coverage gaps remain

Status: resolved (2026-03-17)

Severity: low

Summary:
There are a few clear cleanup items that are not production bugs on their own, but they reduce confidence and make maintenance noisier.

Evidence:
- Ruff currently fails on unused imports:
  - `tests/unit/test_config.py:8-16`
  - `tests/unit/test_webhook_payload.py:5`
- Scheduler startup selects an unused `config_yaml` column:
  - `src/scrapeyard/scheduler/cron.py:82-89`
- Scheduled-job tests only cover create/list/delete, not actual execution semantics:
  - `tests/integration/test_scheduled_job_lifecycle.py:23-49`
- Health tests do not exercise repeated lifespan startup, so the duplicate-logger issue is invisible:
  - `tests/unit/test_health.py:9-35`

Resolved by cleaning the Ruff failures in tests, removing the dead `config_yaml` read from scheduler startup, and adding focused tests for scheduled trigger behavior and logging setup. Remaining test gaps are now tracked by the deferred queue/lifecycle items rather than this hygiene bucket.
