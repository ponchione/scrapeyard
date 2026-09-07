# Codebase audit: technical debt and architecture follow-ups

Originally audited on **2026-09-07**, against commit
`e8b505cb205272231a9810e86ec3ea33bddf2e9c` on the unmerged
`agent/production-readiness` branch. All outstanding entries were rechecked on
**2026-09-07** against `main` at `5be6e476c8fc378be7baffc05c19371de97b863c`.
The five findings below remain outstanding; resolved findings and observations
specific to the other branch have been removed. The original documentation
recheck did not establish that findings on the unmerged branch were fixed.

The sweep covered API authentication and validation, configuration and transforms,
HTTP/browser fetching, execution budgets, queue and scheduler lifecycle,
SQLite transactions, result persistence and cleanup, webhook delivery, runtime
supervision, and build/security configuration. TD-12 through
TD-14 are architecture follow-ups: their implementation observations are verified,
but overload consequences and performance gains have not been measured. The
Eyebox consumer review adds TD-16 (a completeness-contract mismatch) and TD-17
(verified default limit differences requiring joint qualification). TD-14 is a
proposed design simplification, not a reproduced correctness defect. Priorities:
**P1** = security or service availability; **P2** = functional or operational
correctness; **P3** = efficiency or maintainability improvement, with measurement
required where the benefit is still a hypothesis.

## Verification

| Check | Observed result on current `main` |
| --- | --- |
| `poetry check --lock` | Passed |
| `poetry run ruff check src tests scripts` | Passed |
| `poetry run mypy --no-incremental src` | Passed: 94 source files |
| `poetry run pytest -W error` | 2,039 passed, 15 skipped; 88.78% coverage; 169.11 seconds |
| Focused unit tests for browser/debug, CI, readiness, databases, cleanup, pagination, memory, config, and imports | 550 passed across 12 modules |

The full suite ran against the current application code before this documentation
recheck. The lock, Ruff, mypy, and focused tests were rerun during the recheck.
A synthetic capped target still finalized successfully with a next link.

The current browser adapter does not capture fetch/XHR response bodies. Repeated
SQLite readiness probes reuse cached connections and execute only `SELECT 1`;
tracing nine probes found no integrity scans or schema writes. The expired browser
review policy and validator belong to the unmerged branch and are absent from
`main` and its CI/Docker build. Their removal from this list does not attest that a
browser security review was completed.

The local environment uses Python 3.12. The full suite's 15 skips are live Redis
tests. Real Redis/browser execution, container builds, host firewall installation,
release qualification, and a fresh external dependency/CVE audit were not run
for this recheck.

The Eyebox adapter, removal policy, configuration defaults, and deployment runbooks
were inspected read-only in the checkout at `d2cbb59`. They still support the
remaining consumer findings, and the joint deployment qualification worksheet is
still pending. Eyebox tests were not rerun. Config inventory describes checked-in
workloads, not the active production allow-list or measured traffic.

## Eyebox consumer assessment

Eyebox is currently the only consumer. Its scheduled/admin path submits committed
YAML to `POST /scrape` with an idempotency key, polls `/jobs/{id}`, and downloads
`/results/{id}`. Eyebox owns scheduling, ingest recovery, catalog writes, and
business data in Postgres. Scrapeyard owns scrape execution and artifacts. The
scheduled path strips webhooks and does not register native Scrapeyard cron jobs;
some YAML files still contain schedule blocks, but `/scrape` creates ad-hoc runs.
See [Eyebox topology](../eyebox/docs/deployment-topology.md) and
[scheduled client](../eyebox/ingest/scheduled_scrapeyard.py).

Keep this ownership and the separate single-host Scrapeyard deployment. Scraper
downtime primarily delays freshness of Eyebox's persisted catalog. Native cron and
webhook expansion, horizontal scaling, and metadata consolidation have no new
consumer requirement from this review. Existing supported behavior remains intact.

For consumer-facing work, address TD-16's removal-safety contract first and qualify
TD-17's limits alongside it. TD-12's browser memory protection remains important,
while the normal scheduler reduces the urgency of a scrape backlog cap. TD-13 is
the strongest performance candidate after correctness/capacity work. TD-14 is
deferred until persistence maintenance or measured contention justifies a migration.

## Findings

| ID | Priority | Finding |
| --- | --- | --- |
| TD-12 | P1 memory / P2 backlog | Scrape admission lacks a backlog cap and browser-aware memory headroom |
| TD-13 | P3 | Page fetches repeatedly create HTTP clients and browser sessions |
| TD-14 | P3, deferred | Separate metadata databases expand persistence coordination |
| TD-16 | P2 | Successful capped scrapes can authorize incorrect Eyebox listing removals |
| TD-17 | P2 | Eyebox and Scrapeyard limits lack a jointly qualified operating envelope |

### TD-12 — Scrape admission lacks a backlog cap and browser-aware memory headroom

**Locations:** `src/scrapeyard/queue/pool.py:221`, `:319`, `:456`;
`src/scrapeyard/queue/memory.py:13`;
`src/scrapeyard/api/scrape_submission.py:59`;
`src/scrapeyard/scheduler/cron.py:290`; `docker-compose.yml:38`, `:80`.

**Eyebox assessment:** split urgency: browser-aware memory protection remains P1;
the backlog cap is P2 for this consumer. Eyebox's scheduler defaults to four
concurrent dispatch workers, each synchronously waiting for its scrape/ingest
cycle, with durable deduplication and bounded redispatch. That reduces routine
queue growth. It does not impose a global Scrapeyard cap: admin/smoke submissions
and remote runs still executing after client timeouts can add work. Size the cap
from the qualified allow-list and observed queue age, not the total config count.
Eyebox already retries 429/503 with bounded `Retry-After` handling. References:
`../eyebox/ingest/scheduler.py:612`, `../eyebox/ingest/config.py:52`, and
`../eyebox/ingest/scrapeyard_transport.py`.

**Observed design:** worker and browser concurrency are bounded, but accepting a
new scrape does not enforce a maximum pending backlog. Request rate limiting
does not account for scrape duration or the amount of already-accepted work.
`_check_memory()` runs only during enqueue and reads `/proc/self/statm`, so it
excludes browser subprocesses. Already-queued work can continue starting after
that check. Compose limits Redis to 512 MiB and the application container,
including its browsers, to 4 GiB. The Compose enqueue threshold is 3 GiB of
Python RSS, leaving nominal headroom but still excluding browser memory. These
settings do not provide a backlog cap or check container usage before execution.

**Impact:** sustained submissions faster than completion can grow Redis and
SQLite state and queue latency. Browser memory can exhaust the application
container while Python's RSS remains below its configured threshold, affecting
the API, scheduler, and maintenance as well as workers. This is an exposure
identified from the control flow; the audit did not reproduce an OOM.

**Fix direction:** enforce one atomic capacity reservation for new accepted runs
across ad-hoc, scheduled, and manual submission. Define whether the configured cap
counts pending runs or all nonterminal runs; a read-then-enqueue depth check alone
can oversubscribe under concurrent submissions. Preserve idempotent replay and
avoid stranded job/config/idempotency rows when rejecting admission. Recovery of
an already-accepted run must not consume a second reservation; cancellation and
draining existing work must remain possible when full. Return a documented
retryable overload response and record scheduled admission failures durably.

Use container/cgroup memory usage, with a documented fallback outside containers,
and reserve headroom before starting more browser work. Keep the hard container
limits and existing browser permits. Put any new limits in `ServiceSettings` as
`SCRAPEYARD_*` settings, and expose admission rejections/headroom in existing
metrics.

**Acceptance checks:** extend `tests/live_redis/test_queue_lifecycle.py` with slow
workers and concurrent submissions exceeding a small configured cap. Assert the
cap, retryable rejection, eventual draining, and no duplicate reservation on
idempotent replay or missing-delivery recovery. Cover scheduled/manual admission
and cancellation while full. Extend `tests/unit/test_queue_memory.py` and pool
tests for high container usage with low Python RSS; add a bounded container load
case in `tests/qualification/run_qualification.py` that verifies admission slows
before memory exhaustion and resumes after capacity returns. Include Eyebox's
idempotent retry path so overload does not create a new remote job per attempt.

### TD-13 — Page fetches repeatedly create HTTP clients and browser sessions

**Locations:** `src/scrapeyard/engine/basic_fetch.py:143`;
`src/scrapeyard/engine/browser_debug.py:699` and the installed Scrapling engines;
`src/scrapeyard/engine/scraper.py:219`, `:618`;
`src/scrapeyard/engine/pagination.py`.

**Eyebox assessment:** this is now the strongest performance candidate, still P3
until measured. Its 46 checked-in configs all select async execution; 205 of 208
targets use browsers (139 dynamic, 66 stealthy), and 39 configs paginate. For
example, Brownells configures 11 sequential targets with a combined ceiling of
41 pages. These are configured ceilings, not observed page counts or active
production load. Benchmark representative jobs from the qualified launch
allow-list before considering broad pooling. References: `../eyebox/configs/`
and `../eyebox/configs/brownells-optics.yaml`.

**Observed design:** every basic request creates and closes an `AsyncHTTPTransport`
and `AsyncClient`, including individual redirect hops. Each local Chromium fetch
launches a browser and context through Scrapling's `PlaywrightEngine`;
`CamoufoxEngine` enters `AsyncCamoufox` inside each fetch. Pagination repeatedly
calls these fetch paths, so normal local-browser pagination pays session/browser
setup for each page and discards session state.
Externally attached CDP browsers are a separate case; a new session does not
necessarily launch a new external browser process.

**Impact:** avoidable connection establishment and browser startup are likely
throughput costs, especially for many pages on the same site. Their magnitude
has not been benchmarked. HTTPX documents the benefits of keeping a
[client and its connection pool alive](https://www.python-httpx.org/advanced/clients/).

**Fix direction:** first benchmark representative basic and browser pagination
against a controlled site. If setup is material, keep an HTTP client/transport
and browser session alive for one target's pagination run, with explicit cleanup
on success, failure, cancellation, and budget exhaustion. Prefer this bounded
lifetime before adding a process-wide browser pool. Define retry behavior so a
damaged session can be discarded and rebuilt.

Preserve all per-request destination checks, pinned-IP/Host/SNI behavior, proxy
selection, redirect credential scoping, rate limiting, budgets, and browser
sandbox/network guards. Pooling must respect logical origin as well as the
validated connection endpoint; distinct hosts can resolve to the same IP. Keep
cookies and browser state isolated between unrelated targets/runs, and specify
how state persists between pages and is reset between retry attempts. Reusing a
session must not leave callbacks bound to an earlier page's diagnostics or budget.

**Acceptance checks:** record before/after elapsed time, physical connection and
browser-launch counts, and peak container memory for the same multi-page workload.
Extend basic streaming, pagination, and browser fetcher tests to prove reuse and
cleanup, including cancellation and a broken-session retry. Run existing live
browser guards and add a cross-origin/cross-run credential-isolation case. Retain
the change only if it reduces setup cost without weakening those guarantees.

### TD-14 — Separate metadata databases expand persistence coordination

**Locations:** `src/scrapeyard/storage/database.py:20`;
`src/scrapeyard/storage/result_store.py:393`;
`src/scrapeyard/storage/job_store.py:1557`;
`src/scrapeyard/queue/worker.py:533`, `:584`;
`src/scrapeyard/api/job_lifecycle.py:115`;
`scripts/qualification_backup.py`; `docs/SCALING.md`.

**Eyebox assessment:** deferred, P3. Eyebox consumes HTTP status/results and stores
its catalog in its own Postgres database; it has no dependency on Scrapeyard's
internal database layout. Consolidation has little immediate consumer benefit.
Revisit only when cross-store maintenance is materially costly or measurements
justify changing persistence. This does not propose sharing Eyebox's database
with Scrapeyard or migrating Scrapeyard to Postgres.

**Observed design:** jobs/runs/idempotency/webhook intents live in `jobs.db`,
errors in `errors.db`, and result metadata in `results_meta.db`. The worker writes
the artifact and result metadata before a separate jobs.db transaction finalizes
the run and webhook intent. Deletion, retention, recovery, and backups must
coordinate these separate stores. Existing reconciliation and resumable deletion
handle many of these gaps; this entry does not assert that they are missing.

**Impact:** the single-process deployment carries additional partial-failure
states and cross-store orchestration. Consolidation is a maintainability candidate,
not a promised speedup: one SQLite writer may introduce more contention than
the current three independently serialized connections.

**Fix direction:** evaluate this at the next planned persistence refactor. Keep
JSON/browser artifacts on disk, but consider one SQLite database for relational
state. Make successful run finalization, result metadata publication, and webhook
intent creation one ownership-checked transaction after the artifact is written.
Placing tables in one file is insufficient if the existing stores continue to
commit each phase independently; expose the combined operation through the
storage interface. Preserve legitimate retained results after job deletion.

Plan a resumable migration of existing databases, including migration ledgers,
encrypted values and their encryption context, indexes, and retained records.
Update `sql/*.sql`, stores/protocols, tests, health probes, backup manifests and
restore/relocation tooling together. Do not rewrite applied migration history.
Keep Redis delivery and filesystem reconciliation: consolidation cannot make
those systems part of a SQLite transaction or make horizontal scaling safe.

**Acceptance checks:** migrate populated legacy state containing queued/running
jobs, terminal runs, pending webhook intents, and results retained after deletion.
Interrupt migration and verify safe restart without loss or duplication. Use
fault injection between artifact writing and the combined metadata commit to
prove that terminal state/result metadata/webhook intent commit together or roll
back together. Rerun cancellation, retention, recovery, and backup/restore
qualification. Compare writer contention and API latency under the existing load
workload before deciding whether consolidation is worth the migration cost.

### TD-16 — Successful capped scrapes can authorize incorrect Eyebox listing removals

**Priority/ownership:** P2; first consumer-correctness follow-up. Requires a
Scrapeyard result-contract change and an Eyebox adapter/removal-policy change.

**Locations:** `src/scrapeyard/engine/pagination.py:149`;
`src/scrapeyard/engine/scraper.py:653`;
`src/scrapeyard/queue/worker.py:1152`, `:1174`;
`../eyebox/ingest/sources/scrapeyard.py:306`, `:2227`;
`../eyebox/ingest/pipeline/runner.py:901`;
`../eyebox/ingest/pipeline/removal.py:17`.

**Observed contract:** reaching `pagination.max_pages` stops pagination without
marking the target failed or the run partial, even if another next-page link
exists. Normal finalization can therefore report `complete` for a bounded sample.
The result has diagnostic page information but no explicit coverage-completeness
contract. Eyebox unwraps the listing array and sets
`completed_categories=requested_categories` whenever the job status is `complete`.
Its removal policy accepts that assertion and marks previously active listings
absent from the returned set inactive within the authorized scope.

**Local reproduction:** a synthetic target with `max_pages=1`, one scraped page,
one record, and a next-page link retained `pagination_next_count=1` but finalized
as `complete`; the next fetch was not called. Independently, passing a `complete`
payload through Eyebox's real adapter and `_removal_scope()` returned
`(True, ['red-dots'])` without any evidence that pagination was exhausted.
Both checks used mocked source data and no network/database writes. Actual
production deactivations were not reproduced.

**Impact:** a product moving beyond a page cap can appear removed even though it
is still offered. This matters for price comparison and availability. Existing
Eyebox safeguards correctly suppress removals for explicitly `partial` results
and unscoped fetch errors, but do not cover successfully executed samples. Keeping
the page cap is valid; equating execution success with a full snapshot is the gap.

**Fix direction:** expose per-target pagination stop reason and exhaustion or
truncation evidence in the result contract, independently of execution success.
Distinguish exhausted pagination from page limits, repeated URLs, unsafe next
links, and unknown coverage. Eyebox must preserve and consume that metadata,
map it to its declared source/category scope, and require affirmative snapshot
coverage before authorizing removals. Eyebox owns whether the configured target
set is an exhaustive snapshot or a deliberate sample; Scrapeyard should not
infer retailer/category business semantics. Missing evidence and capped/sampled
coverage must suppress absence-based removals while allowing useful returned
listings to be updated. Account for multiple targets contributing to one scope.

**Acceptance checks:** add a contract fixture where a valid next link remains at
the cap and verify that the result remains usable but cannot authorize removals.
Seed a previously seen listing beyond the cap and prove it stays active after
ingestion. Pair that with an explicitly exhaustive snapshot that legitimately
removes a missing listing. Cover mixed complete/truncated targets in one category,
partial failures, loops, and old results lacking the new metadata. Extend
`tests/unit/test_scraper_pagination_helpers.py` and Eyebox's
`ingest/tests/test_scrapeyard_source.py`, `test_runner.py`, and `test_removal.py`;
use a shared serialized contract fixture across both repos without cross-service
production imports. The existing
`test_fetch_result_declares_source_completeness` only distinguishes top-level
`complete` from `partial` and must cover the additional evidence.

### TD-17 — Eyebox and Scrapeyard limits lack a jointly qualified operating envelope

**Priority/ownership:** P2; joint deployment/configuration qualification. The
defaults below are verified; an active production configuration failure was not.

**Locations:** `src/scrapeyard/common/settings.py:44`, `:63`;
`../eyebox/ingest/config.py:37`, `:43`, `:48`, `:53`;
`../eyebox/ingest/scheduled_scrapeyard.py:365`;
`../eyebox/ingest/sources/scrapeyard.py:194`, `:314`;
`../eyebox/docs/deployment-topology.md`.

| Limit | Scrapeyard default | Eyebox default | Consequence to qualify |
| --- | --- | --- | --- |
| Execution/wait duration | 900 seconds per run, after queueing | 300 seconds per polling window | A polling window can expire while the accepted run remains queued/running. |
| Result size | 50 MiB serialized artifact | 10 MiB downloaded response | A successful result can exceed the consumer's download cap; include response-envelope overhead. |
| Record count | 100,000 extracted records per run | 5,000 input listings | A successful result can be rejected by the consumer's listing cap. |

Eyebox already retries transport failures and preserves the logical run ID for
bounded delayed redispatch (two by default). A polling timeout alone therefore
does not establish lost work or duplicate execution. Result byte/listing cap
violations are permanent errors, however; retrying the same result cannot fix
them. Queue wait, HTTP retry delays, execution, and result transfer all contribute
to the end-to-end time even though their individual budgets differ.

**Fix direction:** qualify one operating envelope for the exact launch allow-list:
maximum useful records/bytes, acceptable queue age, run duration, transfer time,
and recovery interval. First align existing deployment settings and split large
retailer jobs into bounded scopes when appropriate; this does not require another
queue, scheduler, or shared database. Either constrain Scrapeyard outputs to what
Eyebox accepts or explicitly raise Eyebox's limits after memory/processing tests.
Size the client wait and total redispatch window from qualified queue-plus-run
timing. Keep source configs stable for a logical retry, and keep Scrapeyard's
idempotency and result retention longer than the supported recovery window.
Document the paired settings in both deployment runbooks and verify them during
preflight; avoid silently raising every limit to its maximum.

**Acceptance checks:** exercise jobs that exceed 300 seconds but complete within
the configured server budget, including queue delay, a lost submit response,
ingest restart, and interrupted result download. Verify one remote job per logical
run and eventual ingestion or a visible bounded failure. Test payloads just below
and above the agreed byte and record limits, with matching server/client behavior
and no unsafe removal on rejected data. Record elapsed time and peak memory for
the qualified retailer set. Extend Eyebox's scheduled-client/source tests and the
existing cross-provider restart/network-interruption qualification; mocked unit
tests alone do not establish the deployed envelope.
