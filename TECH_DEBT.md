# Codebase audit: technical debt and architecture follow-ups

Audited on **2026-09-07**, against commit
`e8b505cb205272231a9810e86ec3ea33bddf2e9c`.
This is an audit report; application code was not changed.

The sweep covered API authentication and validation, configuration and transforms,
HTTP/browser fetching, execution budgets, queue and scheduler lifecycle,
SQLite transactions, result persistence and cleanup, webhook delivery, runtime
supervision, and build/security configuration. Findings through TD-11 have either a
local reproduction or a directly verifiable failing check/reference trace.
TD-12 through TD-15 are architecture follow-ups from the same audited revision:
their implementation observations are verified, but overload consequences and
performance gains have not been measured. The Eyebox consumer review adds TD-16
(a locally reproduced completeness-contract mismatch) and TD-17 (verified default
limit differences requiring joint qualification). TD-14 is a proposed design
simplification, not a reproduced correctness defect. Priorities:
**P1** = security or service availability; **P2** = functional or operational
correctness; **P3** = efficiency or maintainability improvement, with measurement
required where the benefit is still a hypothesis.

## Verification

| Check | Observed result |
| --- | --- |
| `poetry check --lock` | Passed |
| `poetry run ruff check src tests scripts` | Passed |
| `poetry run pytest` | 2,139 passed, 19 skipped; 88.64% coverage; 176.65 seconds |
| `SCRAPEYARD_RUN_LIVE_BROWSER=1 poetry run pytest -W error --no-cov -m live_browser tests/live_browser -q` | 3 passed |
| `poetry run mypy src/scrapeyard` | Failed: three errors in `engine/browser_debug.py` |
| `poetry run python scripts/audit_browser_security.py --dockerfile Dockerfile` | Failed: browser security review expired |

The local environment uses Python 3.12. The default suite's skips include live
Redis tests; real Redis execution, container builds, host firewall installation,
release qualification, and a fresh external dependency/CVE audit were not run.
The expired review finding below is **not** an assertion of a particular browser
vulnerability. Existing accepted debt in [docs/TECHNICAL_DEBT.md](docs/TECHNICAL_DEBT.md)
has not been relabeled as a newly discovered defect.

The architecture follow-up also passed `poetry run ruff check src tests` and
120 focused tests covering instance ownership, leases, terminal webhook atomicity,
worker budgets, the pool, and scrape/schedule/cancellation integration. That pass
did not rerun live Redis/browser tests or load qualification.

The Eyebox review used its checkout at commit
`0cbfd53f133c58e94a80a01154a1e65c2c0f2ce2`. From `../eyebox/ingest`,
`./.venv/bin/python -m pytest tests/test_scheduled_scrapeyard.py tests/test_scheduler.py tests/test_scrapeyard_source.py tests/test_removal.py --rootdir=. -q`
passed **120 tests**. Separate synthetic checks reproduced both sides of TD-16
without network requests or database writes. Config inventory and code references
describe the checked-in workload, not the active production allow-list or measured
traffic. No live scraping, production inspection, or new load benchmark was run.

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
the strongest performance candidate after correctness/capacity work. TD-15 remains
an operational improvement; TD-14 is deferred until persistence maintenance or
measured contention justifies a migration.

## Findings

| ID | Priority | Finding |
| --- | --- | --- |
| TD-03 | P2 | Cleanup treats a normal deadline-limited partial page as a failure |
| TD-04 | P2 | Directory fanout can permanently prevent artifact reconciliation |
| TD-05 | P2 | Browser debug capture reads bodies before checking eligibility or capacity |
| TD-06 | P2 | Transform parsing removes meaningful whitespace inside quoted arguments |
| TD-08 | P2 | The required browser security review has expired |
| TD-09 | P2 | The current source fails the required type-check gate |
| TD-10 | P3 | Transform pipeline scanning repeatedly copies growing prefixes |
| TD-11 | P3 | Superseded internal helpers have no runtime callers |
| TD-12 | P1 memory / P2 backlog | Scrape admission lacks a backlog cap and browser-aware memory headroom |
| TD-13 | P3 | Page fetches repeatedly create HTTP clients and browser sessions |
| TD-14 | P3, deferred | Separate metadata databases expand persistence coordination |
| TD-15 | P2 | Readiness performs database-wide integrity scans on every request |
| TD-16 | P2 | Successful capped scrapes can authorize incorrect Eyebox listing removals |
| TD-17 | P2 | Eyebox and Scrapeyard limits lack a jointly qualified operating envelope |

### TD-03 — Cleanup treats a normal deadline-limited partial page as a failure

**Locations:** `src/scrapeyard/storage/cleanup.py:88`, `:131`, `:835`;
`src/scrapeyard/storage/result_store.py:1185`.

Artifact reconciliation intentionally returns a short page with
`metadata_scan_exhausted=False` or `filesystem_scan_exhausted=False` when its
deadline expires. `_CleanupCycleBudget.record()` rejects exactly that result:
if `count < limit` and `has_more` is true, it raises `RuntimeError`.

This turns expected bounded progress into `CleanupIncompleteError`, marks cleanup
health as failed, and takes the ordinary cleanup delay instead of the successful
saturated-cycle catch-up delay (defaults: six hours versus five seconds).

**Reproduced:** create three real result artifacts, use batch size three, and
advance a controlled monotonic clock past the deadline after reading the first
artifact. Calling `_drain_artifact_reconciliation()` with the real `LocalResultStore`
raises:

```text
RuntimeError: Cleanup phase 'artifact_metadata' reported more work after a short page
```

The existing
`test_reconciliation_deadline_preserves_unvalidated_metadata_rows` in
`tests/unit/test_result_store_cleanup.py` already establishes the valid short-page
store behavior. The cleanup-layer time-ceiling test in `tests/unit/test_cleanup.py`
uses a full page, so the two contracts are never exercised together.

**Fix direction:** accept partial progress when reconciliation reports remaining
work and the cycle deadline has been reached; mark the cycle saturated and preserve
its durable cursor. Keep the safeguards against invalid counts and avoid a busy
loop on zero-progress pages.

**Regression check:** exercise the cleanup orchestrator and real store together,
stopping midway through metadata and filesystem pages. Expect a successful,
saturated outcome and continuation from the saved cursor on the next pass.

### TD-04 — Directory fanout can permanently prevent artifact reconciliation

**Locations:** `src/scrapeyard/storage/result_store.py:743`, `:822`;
`src/scrapeyard/storage/filesystem.py:240`;
`src/scrapeyard/api/dependencies.py:82`.

`_directory_entries()` rejects a directory once it has more than
`max_artifact_tree_entries` children. `_scan_artifacts()` applies that per-tree
limit to the result root, project directories, and job directories before applying
its run cursor. Consequently, a project with more than 10,000 job directories
(the default configured limit) cannot be scanned at all. Raising the batch size
does not help.

This is reachable through ordinary ad-hoc usage over time: every submission has a
unique job name, while result deletion removes run directories and leaves their
empty job/project parents. Retention therefore does not bound project fanout.
Once the limit is crossed, orphan and temporary-file reconciliation under that
project repeatedly fails, even when most job directories are empty.

**Reproduced at a small equivalent limit:** instantiate `LocalResultStore` with
`max_artifact_tree_entries=2`, create `project/job-{0,1,2}/run`, and invoke
`_scan_artifacts(..., limit=2, after=None, deadline_reached=lambda: False)` twice.
Both passes return zero entries and `exhausted=True`, with:

```text
ReconciliationOperationFailure(action='scan_project', identifier='project',
                               error_type='DirectoryEntryLimitExceeded')
```

**Fix direction:** separate the per-run artifact safety ceiling from enumeration
of project/job/run namespaces. Enumeration must make bounded cursor progress
through large parent directories instead of rejecting their entire contents.
Also remove empty parent directories opportunistically where safe; use `rmdir`
and tolerate non-empty/racing directories, never recursive parent deletion.

**Regression check:** exceed the configured fanout using both empty job directories
and jobs with orphan runs; repeated cleanup passes must eventually visit every run.
Retain tests that reject oversized individual artifact trees and symlink traversal.

### TD-05 — Browser debug capture reads bodies before checking eligibility or capacity

**Location:** `src/scrapeyard/engine/browser_debug.py:278`, particularly lines 291–303.

With `SCRAPEYARD_BROWSER_DEBUG_ENABLED=true`, `capture_response_bodies()` calls and
awaits every retained fetch/XHR response's `body()` before checking whether its
content type is supported or debug space remains. It then decodes/redacts the full
payload before asking the budget for storage capacity. Even an already-exhausted
debug budget continues downloading and processing bodies that will be discarded.

**Reproduced:** fully reserve a one-byte debug budget, enqueue a response with
`content_type='application/octet-stream'`, and let its async `body()` record calls.
Capture calls `body()` and only afterward reports
`body_capture_skipped='non-text content type'`. The same ordering is present for
text bodies rejected by the byte budget.

**Impact:** avoidable IPC, memory allocation, and CPU work for binary/oversized
responses and after debug capacity is exhausted. A large body is fully materialized
before its size can cause omission; the debug byte setting bounds stored output,
not these allocations. These reproduction checks did not attempt to cause OOM.

**Fix direction:** reject unsupported content types and exhausted capacity before
requesting bodies. Bound capture of eligible bodies before full materialization;
a `Content-Length` precheck alone cannot establish that bound for missing or
misleading lengths. If the browser API cannot provide bounded capture, omit such
optional bodies or capture them through a transport that can enforce the limit.
Keep supplementary diagnostics from consuming the rest of an otherwise useful run.

**Regression check:** `body()` must not be invoked for binary responses or when
capacity is exhausted. Include oversized and unknown-length text responses, with
an explicit assertion about the capture limit rather than just the final file size.

### TD-06 — Transform parsing removes meaningful whitespace inside quoted arguments

**Location:** `src/scrapeyard/config/transforms.py:234`.

`_parse_args()` uses `csv.reader`, then calls `.strip()` on every decoded argument.
CSV parsing has already removed the quotes, so the later strip cannot distinguish
syntactic whitespace from literal whitespace that the caller quoted intentionally.

**Actual results:**

| Expression and input | Current output | Expected literal-string behavior |
| --- | --- | --- |
| `replace(" ", "_")` on `a b` | `_a_ _b_` | `a_b` |
| `append(" kg")` on `10` | `10kg` | `10 kg` |

The first case is particularly damaging: stripping the single-space search argument
turns it into the empty string, making replacement insert text at every boundary.

**Fix direction:** preserve whitespace inside quoted arguments. Continue accepting
ordinary separator whitespace in the documented function syntax. Reuse the CSV
parser where it suffices; avoid introducing another transform language.

**Regression check:** add these two cases alongside
`test_replace_func_syntax_preserves_quoted_commas` in `tests/unit/test_config.py`,
including a quoted replacement containing leading/trailing spaces and the complete
selector pipeline path.

### TD-08 — The required browser security review has expired

**Locations:** `security/browser-policy.json:4`;
`scripts/audit_browser_security.py:43`;
`.github/workflows/ci.yml:55`; `Dockerfile:169`.

The committed review expires on **2026-08-15**. On the audit date, the required
command exits with status 2:

```text
Browser security review expired on 2026-08-15 (as of 2026-09-07)
```

CI explicitly runs this check, and an uncached Docker build invokes the same policy
validator. The review gate currently prevents those paths from completing.

**Fix direction:** perform and record the actual browser/dependency security review,
refreshing versions, hashes, manifests, and policy dates as its findings require.
Do not just extend the expiration or disable the check. This audit did not establish
whether the pinned browser releases have a specific current vulnerability.

**Validation:** run the validator using the real current date, then the existing
browser security tests, runtime manifest checks, and container qualification for
any changed browser/runtime inputs.

### TD-09 — The current source fails the required type-check gate

**Locations:** `src/scrapeyard/engine/browser_debug.py:137`, `:307`, `:829`;
`.github/workflows/ci.yml:57`.

`poetry run mypy src/scrapeyard` reports:

```text
browser_debug.py:137: Item "None" of "Any | None" has no attribute "items" [union-attr]
browser_debug.py:307: Argument 1 to "_debug_limit_diagnostic" has incompatible type
                     "RunBudget | None"; expected "RunBudget" [arg-type]
browser_debug.py:829: Argument 1 to "_debug_limit_diagnostic" has incompatible type
                     "RunBudget | None"; expected "RunBudget" [arg-type]
```

These are verified CI failures, not three additional proven runtime crashes.
The latter two depend on relationships between `granted`, payload size, and budget
presence that the type checker cannot infer. The first relies on a `hasattr` check
that does not narrow this optional value sufficiently.

**Fix direction:** express the narrowing explicitly: validate the header mapping
type and keep budget-dependent diagnostics within a non-optional budget branch.
Avoid broad `Any` casts or suppressing type errors across the module.

**Validation:** `poetry run mypy src`, Ruff, and the browser debug unit tests.

### TD-10 — Transform pipeline scanning repeatedly copies growing prefixes

**Location:** `src/scrapeyard/config/transforms.py:56`.

Every character iteration computes `raw[start:index].strip()` before even handling
the already-open-quote case. A long single step therefore repeatedly copies its
growing prefix, making scanning quadratic in the step length. The pipeline-step
limit is applied only after scanning. Submission validation reaches this code
before the queued run's duration budget exists.

**Local measurements**, calling `split_transform_pipeline('append("' + 'x' * n + '")')`:

| Argument length | One observed scan |
| --- | --- |
| 20,000 | 0.0037 seconds |
| 40,000 | 0.0112 seconds |
| 80,000 | 0.0364 seconds |
| 160,000 | 0.1146 seconds |
| 250,000 | 0.2549 seconds |

These are local diagnostic timings, not production throughput claims. The largest
example fits within the default request body ceiling with a small YAML envelope;
later argument validation does not undo the scan work.

**Fix direction:** retain the necessary prefix/type state once per step and process
quoted/escaped characters without rebuilding that prefix. Keep the existing tests
for regex alternatives, character classes, doubled quotes, and top-level pipes.
Do not replace it with an unconditional `split('|')`.

**Validation:** rerun the small benchmark and existing transform parser tests;
doubling one long argument should no longer approach four times the scan work.

### TD-11 — Superseded internal helpers have no runtime callers

An AST inventory followed by reference searches across `src`, `tests`, `scripts`,
`security`, and documentation found these conservative deletion candidates:

| Location | Candidate | Evidence and replacement |
| --- | --- | --- |
| `src/scrapeyard/api/serializers.py:215` | `serialize_scrape_result`, `serialize_results_payload` | No callers; both only delegate to `serialize_result_response`, which the active response renderer already calls directly. 30 function lines. |
| `src/scrapeyard/queue/job_state.py:23` | `build_running_job`, `build_completed_job`, `build_failed_job` | Referenced only by their own tests in `tests/unit/test_job_state.py`; durable transitions use the job store's ownership-checked operations. 25 function lines. Keep `run_lease_is_active`. |
| `src/scrapeyard/config/transforms.py:141` | `checked_combined_selector_value_size` | No callers, including tests. Current DOM extraction and transform paths do not use this prospective concatenation helper. 13 function lines. |
| `src/scrapeyard/runtime/health.py:148` | `probe_asyncio_task` | Only its own unit test calls it; production uses `probe_background_service` and supervised monitors. 13 function lines. Its now-private-to-this-helper `BackgroundTask` protocol can also be removed after a final reference check. |

**Fix direction:** delete these helpers and tests whose sole purpose is exercising
the dead helpers. Preserve tests of the active lifecycle, serialization, budget,
and background-health contracts. A caller search must still be rerun at the time
of removal in case intervening work adds consumers.

**Validation:** import-contract tests, Ruff, mypy, and the existing unit/integration
suite. Conservative reduction: **81 production function lines, zero dependencies**,
before removing associated unused imports, blank lines, protocol, or obsolete tests.

Documented `compatibility=legacy-v0` responses, legacy API credentials, database
upgrade paths, alternate IPv4 safety parsing, and adapters needed by the pinned
Scrapling release are reachable behavior. Their names alone do not establish safe
deletion; this report does not recommend removing them without an explicit support
or migration decision.

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
including its browsers, to 4 GiB; these are final resource ceilings, not admission
policies.

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
metrics. This complements TD-05's optional debug-body allocation fix.

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
`src/scrapeyard/engine/browser_fetchers.py:82`, `:157`;
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
and `AsyncClient`, including individual redirect hops. Each Chromium fetch enters
a newly constructed Scrapling session; Camoufox launches a browser inside each
fetch. Pagination repeatedly calls these fetch paths, so normal local-browser
pagination pays session/browser setup for each page and discards session state.
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

### TD-15 — Readiness performs database-wide integrity scans on every request

**Locations:** `src/scrapeyard/main.py:674`, `:819`;
`src/scrapeyard/storage/database.py:573`;
`src/scrapeyard/runtime/health.py:104`; `docker-compose.yml:98`.

**Eyebox assessment:** still P2, secondary to consumer correctness and browser
capacity. Its normal polling calls `/jobs/{id}`, not `/health/ready`, so the
five-second default polling interval does not amplify these integrity scans.
Measure actual monitoring frequency and retained history before prioritizing
the optimization; container/operator readiness checks still exercise this path.

**Observed design:** each `/health/ready` request opens fresh connections to all
three databases, runs `PRAGMA quick_check(1)`, acquires `BEGIN IMMEDIATE`, creates
a temporary-named table in the main schema, and rolls it back. Compose probes
every 30 seconds; additional readiness callers repeat the same work. Public
`/health` and `/health/live` already use the cheap liveness path and are unaffected.

SQLite documents [`quick_check` as O(N)](https://www.sqlite.org/pragma.html#pragma_quick_check)
in database row count. The `(1)` bounds reported errors, not rows scanned. As
history grows, healthy databases can require increasing scan time, and concurrent
probes add read work and compete for the write reservation. This can increase
latency or cause readiness timeouts under load; that growth effect has not been
measured locally.

**Fix direction:** keep a small bounded read/write capability check in readiness
using SQLite's VFS and a rollback-safe operation without per-request schema DDL.
Move full integrity scans to startup and/or supervised periodic maintenance,
with a configured interval, last-success time, and a recorded failure that still
degrades readiness. Define an explicit maximum age for that result so caching
does not silently hide a stopped checker. Coalesce concurrent probe work and
ensure timed-out/cancelled SQLite operations release their connections and locks.
Preserve missing/unwritable database detection, deleted-WAL/SHM diagnostics, and
the existing result-storage/disk checks. Do not reopen live SQLite files with
raw filesystem descriptors; see the accepted portability note in
`docs/TECHNICAL_DEBT.md`.

**Acceptance checks:** populate small and large databases in temporary directories
and compare repeated/concurrent readiness latency while writes continue. Assert
that each readiness call does not rescan the database or create schema objects,
and that concurrent callers do not multiply expensive maintenance scans. Extend
`tests/unit/test_database.py` and `tests/unit/test_health.py` for stale/failed
integrity results, corruption detection through maintenance, probe cancellation,
and missing/unwritable state. Keep the cached-WAL/external-reader regression and
rerun the readiness failure-injection phase of release qualification.

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
