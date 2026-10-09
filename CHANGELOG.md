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
- Added page-parameter pagination (`pagination.page_param`, `page_first`,
  `page_step`) for listings whose pages are addressable by a query parameter.
- Added click pagination (`pagination.mode: click`) for client-rendered
  listings: browser fetchers click the next control in the live page and
  extract each rendered page. Adds the `repeated_page` stop reason.
- Proxy URLs accept a `{session}` placeholder in their credentials, replaced
  by one random token per run for sticky rotating-proxy sessions.
- Added configurable transform pipeline-step and intermediate-value byte caps,
  with environment and Compose wiring.
- Added a deny-by-default, project-scoped allowlist for deployment secret
  references in submitted YAML.
- Added a record/replay page cache (`execution.page_cache: off | record |
  replay`, `SCRAPEYARD_PAGE_CACHE_DIR`) for iterating on selectors offline.
  Replay makes no requests and launches no browser; unrecorded pages fail with
  the `cache_miss` error type and stop reason. Results carry `page_cache` and
  per-target `recorded_at`.
- Added a per-host daily top-level page budget
  (`SCRAPEYARD_DOMAIN_DAILY_PAGE_LIMIT`, `execution.domain_daily_page_limit`)
  and an access-denial cooldown (`SCRAPEYARD_DOMAIN_DENIAL_COOLDOWN_SECONDS`),
  shared across runs and workers through Redis. Stops report the
  `domain_daily_limit` / `domain_cooldown` error types, the `domain_guard`
  stop reason, and `run_budget.domain_guard`; `GET`/`DELETE
  /domains/{host}/guard` inspect and clear a host.
- Added `run_budget.traffic`: requests sent, requests blocked before sending,
  and response bytes received per host (top 25, first- vs third-party totals),
  hostnames only.
- Added opt-in browser subrequest blocking: `browser.block_third_party` aborts
  subrequests outside the target's registrable domain except
  `browser.third_party_allow_hosts`, and `browser.block_url_patterns` aborts
  subrequests matching `*` patterns. Top-level navigations are never blocked.
- `run_budget.traffic.resource_types` and each listed host's `resource_types`
  split requests, blocks and bytes by resource type (`document`, `script`,
  `xhr`, `fetch`, `other`).
- Added opt-in `execution.reuse_browser`: browser targets of a run with the same
  registrable domain, fetcher, proxy and browser settings share one browser and
  context (cookies and storage included) instead of launching one each; every
  fetch still opens a fresh page. With `concurrency` above 1 a group keeps up to
  `concurrency` browsers, one per running target.
- With `execution.reuse_browser`, fresh script and stylesheet responses are
  cached for the run and answered from memory for later pages of the same site
  (after every guard), since Playwright disables the browser's HTTP cache while
  requests are routed. `run_budget.traffic` counts them as `cached`.

### Changed
- A result response now counts against `SCRAPEYARD_API_RESULT_THREAD_MAX_WORKERS`
  until its body has been sent, not just while it renders, so slow downloads
  no longer keep up to 100 MiB per response in memory uncounted. The body is
  sent in 256 KiB pieces paced to the client; a body still unsent after 20 s
  stops counting. Response bytes and headers are unchanged.
- Click pagination keeps each captured page as compressed HTML and parses it
  only while extracting it, instead of holding every page's parse tree until
  the target ends: a 70-page listing replayed from real recordings peaked
  285 MB lower. After a click, a `wait_ms` above 250 now ends once the page
  has gone 250 ms without a DOM change or a pending request (images, fonts
  and stylesheets aside), so a page still loading content keeps waiting, up
  to the full `wait_ms`. Records are unchanged.
- Explicit result deletion (job deletion with `delete_results`, and
  discarding an unowned run's artifact) no longer holds the result metadata
  database while it removes directories, so a large removal no longer stalls
  result saves and `GET /results`. Deletion still removes files first and
  stays retryable.
- Periodic artifact reconciliation no longer re-reads and re-parses every
  retained `results.json` on every pass. Each process parses an artifact once
  and afterwards only checks that its inode, size and timestamps are
  unchanged. Validating 500 artifacts of 410 KB went from 0.61 s to 0.04 s of
  CPU, with no file reads.
- Periodic reconciliation, retention and backlog queries no longer scan
  retained history while holding the shared jobs.db connection. At 25,000
  jobs, terminal webhook reconciliation went from 63 ms to under 0.1 ms,
  stale-queue recovery from 17 ms to under 0.1 ms, scheduled-run retention
  from 67 ms to 3 ms, and ad-hoc retention eligibility from 448 ms to
  10 ms. `GET /errors` filtered by both project and job no longer scans the
  project's errors. Migrations `022`–`024` add an idempotency-by-job index
  and a scheduled-jobs index, and drop six single-column indexes that
  composite indexes already cover. A binary older than this change refuses
  to start on an upgraded database, so rolling back means restoring the
  pre-upgrade backup.
- Result redaction builds each run's secret variants once instead of for
  every string, and leaves records uncopied when the config references no
  secrets. Redacting 100k records took 0.72 s with no secrets and 7.9 s with
  three, and now takes 0.03 s and 1.0 s; the output is unchanged.
- The worker samples memory at most once per queue poll and only when it has
  a free slot, reads the cgroup layout once instead of on every sample, no
  longer re-checks in Redis the jobs it is already running, and no longer
  runs an extra ownership query per target. `scrapeyard_memory_deferrals_total`
  counts at most one worker deferral per poll, and
  `scrapeyard_background_last_success_timestamp_seconds{task="worker"}` is now
  set after each completed poll instead of always reading 0.
- The exact-run poll (`GET /jobs/{id}/runs/{run_id}`), `GET /jobs/{id}`,
  `GET /jobs` and `POST /scrape` no longer decrypt, hash or parse configs on
  the event loop beyond what each needs. An exact-run poll parses each run's
  config once, off the event loop, while still verifying it against its hash
  on every read. Job detail redacts in a thread and caches small results.
  Submissions parse their YAML once, and job listings no longer read stored
  configs. At 110 KB, event-loop time per exact-run poll fell from 43 ms to
  under 1 ms, and per job-detail read from 83 ms to 2 ms. Response bodies are
  unchanged.
- Result JSON is now encoded by the C JSON encoder, one record at a time,
  instead of a pure-Python walk of the whole document. Writing a 10 MB
  result took 327 ms and now takes 30 ms, and rendering a 16.8 MB
  `/results` response took 480 ms and now takes 55-71 ms. The bytes are
  unchanged.
- Click pagination now waits in the page for the items to change (checked every
  50 ms) instead of sleeping in 250 ms steps, so each click resolves as soon
  as the next page renders.
- When the last running job finishes, the worker collects garbage and returns
  freed heap memory to the operating system (glibc `malloc_trim`), so idle
  memory returns near its startup level between jobs.
- Objects built at startup are frozen out of garbage collection (`gc.freeze`),
  which cuts the per-job collection from about 60 ms to 7 ms and lowers
  per-job CPU time.
- Browser configuration is now rejected for `fetcher: basic`, action fields
  that do not apply to the selected browser action are rejected, and YAML
  booleans are no longer coerced into numeric execution controls.
- Result retention settings now require positive values; zero no longer means
  immediate deletion of every eligible result.
- URL grouping now canonicalizes IDNA hostnames and default ports while keeping
  non-default ports as separate protection origins.
- Updated Click from 8.3.1 to 8.4.2 after a newly published command-injection
  advisory caused both development and production dependency gates to fail.
- Restricted stealth browser `additional_arguments` to bounded, typed,
  YAML-safe Camoufox values (`locale`, `fonts`, `custom_fonts_only`, and
  `window`); Python-object fingerprint and screen controls are rejected.
- Expanded regression coverage for browser diagnostics, backup relocation,
  independent cleanup phases, cancellation, and shutdown lifecycle behavior.

### Fixed
- `GET /jobs` and `GET /errors` reject an `offset` above 2^63-1 with a 422
  validation error instead of failing with a 500.
- A schedule update, scheduled job creation or manual trigger that fails and
  is rolled back now logs the cause (job ID and error type) instead of
  returning a bare 503. A manual trigger that hits an encryption key failure
  reports it like every other route.
- A target that fails on a status listed in `retry.retryable_status` now
  reports the last response's `main_document_status`, page title and HTML
  excerpt in `debug`, as other HTTP failures do; it used to report an empty
  debug blob. A retryable 401, 403 or 429 that serves a challenge page is
  now classified as `challenge_page` instead of `blocked_response`.
- Outbound requests now have a total deadline. A webhook receiver or
  basic-fetch server that trickles bytes can no longer hold a request open
  past its timeout: webhook sends fail as a retryable transport timeout, and
  basic fetches fail with the `timeout` error type once
  `SCRAPEYARD_BASIC_FETCH_TIMEOUT_SECONDS` has passed. The timeout used to
  apply only to each connect, write and read.
- A webhook `Retry-After` made of non-ASCII digits (such as `²`) no longer
  turns a retryable 429 or 503 into a permanent failure; it is ignored like
  any malformed value. Webhook connections are now reused: up to 64 KiB of
  the response body is read and discarded instead of the connection being
  dropped after every attempt.
- A failure while recording a run-budget stop, such as a transient database
  error, no longer leaves the run `running` until the heartbeat timeout; the
  run is failed through the normal crash path.
- A cancellation that lands after a run's terminal state has committed no
  longer deletes that run's result artifact, so a `complete` or `partial`
  run keeps its results.
- Periodic queued reconciliation no longer skips stale deliveries on its next
  page after repairing some of the previous one.
- Enqueuing on a worker pool that has not been started, or has already
  stopped, now fails instead of quietly starting a new Redis pool and worker.
- On Python 3.10, egress attestation treats a timed-out challenge connection
  as denied, as it already did on 3.11 and later, instead of failing startup.
- Page-cache replay with `on_empty: retry` no longer updates the live circuit
  breaker from the replayed retry.
- A restart or deploy during a run no longer leaves that run `running` for
  about ten minutes. Shutdown used to spend its whole grace draining active
  jobs and then gave cancelled runs no time to record their failure, so they
  waited for the 600 s heartbeat timeout. Draining now stops when a quarter
  of the grace (at most 5 s) remains, and cancelled runs are marked `failed`
  within that remainder before shutdown continues.
- A cancelled database open or close no longer leaves behind an aiosqlite
  thread that blocks process exit. That could hang shutdown until the
  container was killed, and occasionally hung the test suite after every test
  had passed.
- Startup no longer loads every stored config into memory to verify and
  rotate its encryption: the migration pages through the rows. A steady-state
  startup with 1,000 stored jobs peaked at 135.6 MiB and now peaks at 15.6
  MiB, in the same time. `PRAGMA secure_delete` is now on only while the
  migration rewrites rows. It used to stay on for every later deletion, which
  wrote about 3.5x the WAL bytes per deleted job.
- Browser fetches no longer exhaust `max_requests` far below the number of
  requests sent. Every subrequest reserves room for 20 native redirects, and
  those reservations were returned only when the whole fetch ended; click
  pagination is a single fetch, so long fetches failed with `budget_exceeded`
  (one run counted 10,010 requests with about 5,730 sent). Each redirect
  chain now returns its unused reservation as soon as it ends, and
  `max_requests` remains a hard ceiling. A frame that cannot take the
  request-denying policy while its page closes (it is navigating or busy) no
  longer turns a successful fetch into a failure. A debug screenshot replaced
  by a later page of the same target no longer keeps its bytes charged to the
  browser-debug budget.
- A finished run whose stored config can no longer load, for example after a
  referenced deployment secret was removed, no longer fails terminal webhook
  reconciliation on every pass. That kept `/health/ready` at 503 and stopped
  the service from starting until the job was deleted. When the config
  declares no `webhook` block the run is reconciled as not requiring a
  delivery; runs that declare one still fail closed.
- The test suite no longer depends on the machine it runs on. Scrape admission
  sampled the test runner's real cgroup, so a developer host with a busy cgroup
  refused every scrape and about 150 tests failed; tests now disable memory
  admission unless they set a limit themselves. 57 tests resolved
  `example.com`, `.test` and `.example` names through real DNS; a fake
  resolver now answers them the way public DNS does, offline. Tests that
  inherited the 2 s target stagger, 3 s domain rate limit or 1 s retry backoff
  no longer sleep, and the egress-installer rollback matrix runs its failure
  points concurrently. The suite takes about 45 s instead of 76 s. The
  reachable-challenge egress test no longer races asyncio's accept path and
  leaks a socket that failed an unrelated test under `-W error`.
- Memory admission and browser start checks no longer count the cgroup's
  inactive file cache (`inactive_file`, v1 `total_inactive_file`) as used
  memory. Cached browser, result and log files could push `memory.current`
  above `SCRAPEYARD_WORKERS_MEMORY_LIMIT_MB` in a container without a memory
  limit and make `/scrape` answer 503 while anon memory was low. Anon and active
  file memory still refuse admission.
- A rendered listing that fails only result validation (for example one card
  with an empty required field) is no longer classified as an access denial
  because the page carries challenge, consent or login markers, such as the
  Akamai bot-manager script on every MidwayUSA page. The target now reports
  `content_empty` and follows `validation.on_empty` instead of failing as
  `challenge_page`, skipping the host's remaining targets and starting a
  domain cooldown. Page markers still deny a page with no item-selector
  matches or no field-selector matches, as at fetch time.
- The service no longer keeps the last 128 fetched pages in memory after runs:
  Scrapling's `ResponseEncoding.get_value` memoized every response's full text
  in an `lru_cache`. Scrapeyard now calls it uncached; encodings are unchanged.
- Closed owned subprocess pipes on every egress-probe helper path, including
  assertion and timeout cleanup, so the warning-clean test gate stays clean.
- Allowed source-scoped established return traffic through the deployment
  egress policy while retaining private-destination rejection, and corrected
  the smoke harness to exercise direct-private literal-address validation.
- Made qualification backup migration paths explicit so quiesced backups can
  be validated and restored when the helper is mounted at the container root.
- Refreshed the dated Debian snapshot, upgraded inherited base packages before
  scanning, and made the egress-probe health watchdog reliable under its
  production CPU quota.
- Protected active runs during both age-based and per-job result retention,
  and let webhook workers drain their already-queued batch during shutdown.
- Restored aggregate fetched-byte accounting for real Scrapling response
  objects and made run deadlines return without waiting indefinitely for
  cancellation-resistant awaitables.
- Rejected final-component symlinks during normal result retrieval, matching
  the artifact reconciliation safety policy.
- Treated oversized numeric `Retry-After` headers as out of range, and checked
  webhook delivery age before constructing a potentially overflowing retry
  timestamp.
- Authorized submit/schedule scopes before parsing secret-bearing YAML and
  prevented project-scoped callers from resolving another project's secrets.
- Paced real target starts after concurrency admission, preserved pipes inside
  transform arguments, and marked validation failures resolved after a
  successful retry.
- Reported cancellation-resistant active jobs as incomplete shutdown work and
  consumed cancelled gather results without event-loop callback errors.
- Restored the Python 3.10 test lane by avoiding the Python 3.11-only
  `datetime.UTC` constant.
- Rejected source-tree symlinks before artifact backup and preserved links
  during staging so links discovered while copying are rejected instead of
  dereferenced.
- Corrected backup documentation to permit intentionally retained results
  after their job/run metadata is deleted.
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
