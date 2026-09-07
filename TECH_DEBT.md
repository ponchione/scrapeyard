# Codebase audit: technical debt and architecture follow-ups

Originally audited on **2026-09-07**, against commit
`e8b505cb205272231a9810e86ec3ea33bddf2e9c` on the unmerged
`agent/production-readiness` branch. All outstanding entries were rechecked on
**2026-09-07** against `main` at `5be6e476c8fc378be7baffc05c19371de97b863c`.
The two findings below remain outstanding; resolved findings and observations
specific to the other branch have been removed. The original documentation
recheck did not establish that findings on the unmerged branch were fixed.

The sweep covered API authentication and validation, configuration and transforms,
HTTP/browser fetching, execution budgets, queue and scheduler lifecycle,
SQLite transactions, result persistence and cleanup, webhook delivery, runtime
supervision, and build/security configuration. TD-14 is an architecture
follow-up: its implementation observations are verified, but performance gains
have not been measured. The Eyebox consumer review adds TD-17 (verified default limit differences requiring
joint qualification). TD-14 is a
proposed design simplification, not a reproduced correctness defect. Priorities:
**P1** = security or service availability; **P2** = functional or operational
correctness; **P3** = efficiency or maintainability improvement, with measurement
required where the benefit is still a hypothesis.

## Verification

| Check | Observed result at the documentation recheck |
| --- | --- |
| `poetry check --lock` | Passed |
| `poetry run ruff check src tests scripts` | Passed |
| `poetry run mypy --no-incremental src` | Passed: 94 source files |
| `poetry run pytest -W error` | 2,039 passed, 15 skipped; 88.78% coverage; 169.11 seconds |
| Focused unit tests for browser/debug, CI, readiness, databases, cleanup, pagination, memory, config, and imports | 550 passed across 12 modules |

The full suite ran against the current application code before this documentation
recheck. The lock, Ruff, mypy, and focused tests were rerun during the recheck.
Capped targets still finalize successfully, with explicit non-exhausted pagination
evidence. Eyebox now requires that evidence plus a declared exhaustive target set
before authorizing absence-based removals.

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

For consumer-facing work, qualify TD-17's limits with the explicit pagination
coverage and removal-scope contract documented in [docs/API.md](docs/API.md#pagination-coverage).
TD-14 is deferred until persistence maintenance or measured contention justifies
a migration.

## Findings

| ID | Priority | Finding |
| --- | --- | --- |
| TD-14 | P3, deferred | Separate metadata databases expand persistence coordination |
| TD-17 | P2 | Eyebox and Scrapeyard limits lack a jointly qualified operating envelope |

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
