# Changelog

All notable changes to Scrapeyard are documented here.

Format: [Semantic Versioning](https://semver.org/) — `MAJOR.MINOR.PATCH`.
- **MAJOR** — breaking API or config changes (response shapes, removed endpoints, YAML schema changes)
- **MINOR** — new features, new endpoints, new config options (backwards-compatible)
- **PATCH** — bug fixes, performance improvements, internal refactors

Until 1.0, the API is not considered stable and MINOR bumps may include breaking changes.

---

## Unreleased

### Added
- Added configurable transform pipeline-step and intermediate-value byte caps,
  with environment and Compose wiring.
- Added a deny-by-default, project-scoped allowlist for deployment secret
  references in submitted YAML.

### Changed
- Restricted stealth browser `additional_arguments` to bounded, typed,
  YAML-safe Camoufox values (`locale`, `fonts`, `custom_fonts_only`, and
  `window`); Python-object fingerprint and screen controls are rejected.
- Expanded regression coverage for browser diagnostics, backup relocation,
  independent cleanup phases, cancellation, and shutdown lifecycle behavior.

### Fixed
- Authorized submit/schedule scopes before parsing secret-bearing YAML and
  prevented project-scoped callers from resolving another project's secrets.
- Paced real target starts after concurrency admission, preserved pipes inside
  transform arguments, and marked validation failures resolved after a
  successful retry.
- Reported cancellation-resistant active jobs as incomplete shutdown work and
  consumed cancelled gather results without event-loop callback errors.
- Restored the Python 3.10 test lane by avoiding the Python 3.11-only
  `datetime.UTC` constant.
- Restricted readiness project summaries to each monitoring credential's
  authorized projects.
- Persisted accepted run trigger provenance across queued-delivery recovery,
  including upgrade-safe claiming for legacy queued runs.
- Classified terminal ownership loss as an ignored run instead of successful
  execution.
- Tightened backup artifact path validation and made partial restore installs
  rollback cleanly for a safe retry.
- Closed release-qualification path-filter gaps and corrected targeted-test and
  readiness-status documentation.
- Bounded regex inputs, browser action counts/repeats, selector transform
  pipelines, every intermediate transform value, and blocked-browser request
  diagnostics before they can grow without limit.
- Relocated result metadata safely when a backup is restored to another data
  root, checkpointed restore-only WAL state, and validated the complete schema,
  indexes, idempotency table, and exact migration ledger.
- Enforced one hard monotonic shutdown deadline across cleanup, workers,
  webhooks, HTTP client closure, and database closure, even when an awaitable
  resists cancellation.
- Preserved cancellation and ownership outcomes through scrape submission and
  shutdown instead of misclassifying them as internal failures.
- Isolated cleanup phases so later retention work still runs after a failure,
  and now reports artifact reconciliation operation failures as incomplete.
- Standardized unexpected API failures on the safe versioned error envelope
  and prevented internal exception details from leaking.

## 0.6.0 — 2026-07-12

**Production hardening, durable operations, and API v1 release.**

### Added
- Selector transform helpers for common cleanup chains:
  `collapse_whitespace`, `remove`, `strip_prefix`, `strip_suffix`, `extract`,
  and `default`.
- Long-form `pagination.next` selectors, including XPath support.
- Browser action configs for dynamic/stealthy targets: `click`,
  `wait_for_selector`, `wait_ms`, `scroll`, and bounded `repeat_click`.
- Example configs for consent-banner scrolling and load-more product grids.
- A documented API v1 response contract, named/scoped API credentials,
  idempotent scrape submission, schedule timezones and mutation endpoints,
  Prometheus metrics, readiness probes, and bounded admin pagination.
- Envelope encryption and rotation for persisted proxy, browser-header, and
  webhook secrets.
- Durable webhook retry/outbox processing with attempt and age limits.
- Release qualification, backup/restore validation, live-Redis tests, real
  browser/container smoke tests, dependency auditing, SBOM, and image scanning.
- Run budgets, cancellation/ownership checkpoints, stale-run reconciliation,
  artifact reconciliation, and single-instance filesystem/database guards.

### Changed
- Template and README now document browser actions, typed pagination, and
  practical transform chains.
- Production containers now use digest-pinned images/assets, a dated Debian
  snapshot, hash-checked Python dependency exports, a read-only non-root
  runtime, browser sandbox profiles, and deployment-scoped egress policy.
- Synchronous scrape requests continue through the durable queue/worker path
  and now have explicit queued, replayed, missing-artifact, and error outcomes.
- Result and error APIs redact all configured header values, URL credentials,
  sensitive query values, and internal artifact paths.

### Fixed
- Closed DNS-rebinding windows for direct basic fetches and webhooks by pinning
  validated public IP connections while retaining Host and TLS SNI.
- Removed the duplicate HTTP preflight that changed browser request semantics
  and double-counted fetched bytes.
- Preserved intentionally retained results in backup validation after job
  deletion, and fsynced artifact directories after atomic replacement.
- Corrected cleanup and target outcome metrics so partial cleanup failures and
  pre-result exceptions cannot be reported as successes or cancellations.
- Standardized unexpected API failures on the safe, versioned JSON envelope.

---

## 0.5.1 — 2026-04-09

**Debt-register cleanup and internal maintainability pass.**

### Added
- Shared UTC time helper in `src/scrapeyard/common/time.py`.
- Platform-aware queue memory helper in `src/scrapeyard/queue/memory.py`.
- Small API/storage helper modules for query parsing, response shaping, row mapping, and query construction.

### Changed
- Split worker, scraper, runtime, and storage hot spots into smaller helper modules while keeping public API behavior stable.
- Internal maintainability backlog fully resolved with no active slices.
- Documentation now reflects JSON-only result artifacts and the completed debt-slice cleanup.

### Fixed
- Selector-engine failures now surface as structured failures instead of silently collapsing into empty business results.
- Queue memory admission checks now make Linux-specific `/proc/self/statm` handling explicit and contained.

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
