# Changelog

All notable changes to Scrapeyard are documented here.

Format: [Semantic Versioning](https://semver.org/) — `MAJOR.MINOR.PATCH`.
- **MAJOR** — breaking API or config changes (response shapes, removed endpoints, YAML schema changes)
- **MINOR** — new features, new endpoints, new config options (backwards-compatible)
- **PATCH** — bug fixes, performance improvements, internal refactors

Until 1.0, the API is not considered stable and MINOR bumps may include breaking changes.

---

## 0.5.0 — 2026-03-21

**Run model, webhook dispatch, and API contract stabilization.**

### Added
- **Job run tracking** — `job_runs` table, `JobRun` model, per-run lifecycle
  (create → running → complete/failed/partial) with config hash and error counts.
- **Run-aware API** — `GET /jobs/{id}` returns `runs`, `run_count`, `last_run_at`,
  `next_run_at`. All results and errors tagged with `run_id`.
- **Webhook dispatch** — outbound webhooks on job completion via `HttpWebhookDispatcher`,
  configurable per-job with status filters.
- **Scheduler integration** — `trigger="scheduled"` threaded through run model,
  `get_next_run_time()` exposed via API.
- **Resilience** — circuit breaker, retry handler, fail strategies (`stop`, `continue`, `skip`).
- **Adaptive scraping** — Scrapling adaptive DB, per-project state isolation.
- **Result retention** — automatic cleanup loop with age and per-job pruning.
- **Validation actions** — `warn`, `skip`, `fail`, `retry` on selector mismatch.

### Changed
- **JSON-only output** — removed `formatters/` module and `OutputFormat` enum.
  All results are JSON.
- **Derived stats** — `run_count` and `last_run_at` derived from `job_runs` table
  (no longer stored on the job row).

### Fixed
- APScheduler jitter kwarg compatibility.
- Top-level try/except in `scrape_task` to prevent stuck jobs.
- N+1 cleanup query replaced with single window-function query.

---

## 0.1.0 — 2026-02-28

**Initial scaffold.**

- Project structure, FastAPI app, Scrapling engine, arq worker pool,
  APScheduler cron, SQLite storage, config YAML parsing.
