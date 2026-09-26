# Codebase audit: technical debt and architecture follow-ups

Originally audited on **2026-09-07**, against commit
`e8b505cb205272231a9810e86ec3ea33bddf2e9c` on the unmerged
`agent/production-readiness` branch. All outstanding entries were rechecked on
**2026-09-07** against `main` at `5be6e476c8fc378be7baffc05c19371de97b863c`.
The findings below remain outstanding; resolved findings and observations
specific to the other branch have been removed. The original documentation
recheck did not establish that findings on the unmerged branch were fixed.

The sweep covered API authentication and validation, configuration and transforms,
HTTP/browser fetching, execution budgets, queue and scheduler lifecycle,
SQLite transactions, result persistence and cleanup, webhook delivery, runtime
supervision, and build/security configuration. The Eyebox consumer review adds
TD-17 (verified default limit differences requiring joint qualification). The
[2026-09-07 deployment assessment](docs/deployment-assessment-2026-09-07.md)
extends TD-17 and adds TD-18 through TD-22. Those additions are source and
provider-documentation findings, not results of the earlier test run or a live
deployment audit. Priorities:
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

## Findings

| ID | Priority | Finding |
| --- | --- | --- |
| TD-17 | P2 | Eyebox and Scrapeyard limits lack a jointly qualified operating envelope |
| TD-18 | P1 | Production forward-proxy selection and egress qualification are incomplete |
| TD-19 | P1 | Restricted Hetzner HTTPS ingress and Railway credential handoff are incomplete |
| TD-20 | P1 | Secure startup after host/Docker restart and runtime monitoring are unqualified |
| TD-21 | P1 | Unattended coordinated off-host backup and restoration are incomplete |
| TD-22 | P2 | Deployment and rollback lack retained, qualified release artifacts |
| TD-23 | P3 | The browser URL guard pays a thread hop and a DNS lookup on every subrequest |
| TD-24 | P3 | Browser reuse and its script cache are tuned on fixtures, not real traffic |

For TD-17 through TD-22, read the deployment assessment and the linked source
before implementation. Reuse existing scripts and settings. Record local
implementation checks separately from operator/environment-dependent evidence;
keep an entry open until its deployed acceptance checks pass. Eyebox's
[go-live checklist](../eyebox/GO-LIVE-TESTING-CHECKLIST.md) owns launch gate
closure. Repository edits alone do not provision infrastructure or activate jobs.
TD-23 and TD-24 come from the 2026-09-25 traffic and memory pass (`d0591c7`
through `5de7dc9`); measure before changing anything.

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

**Deployment extension (2026-09-07):** include the actual production resource
and egress configuration. `docker-compose.yml` currently caps Scrapeyard at
4 GiB with a 3,072 MiB admission threshold; `docker-compose.qualification.yml`
allows 8 GiB and disables untrusted-submission mode for fixtures. Fixture
qualification therefore does not establish production browser capacity or proxy
compatibility. Measure the frozen launch set on the chosen x86-64 host, with
TD-18's forward proxy, the intended browser concurrency, and headroom for Redis,
OS, images, artifacts, AOF rewrites, and temporary backups. Record p95/p99 job
and queue timing, cgroup memory/CPU, disk/inode growth, and failure recovery.
Coordinate Eyebox load/soak targets with [item E](../eyebox/TECH-DEBT.md#e-product-outcomes-capacity-and-recovery-qualification)
and transport tests with item D. Record the measured supported settings and
ongoing cost estimate, including proxy bandwidth and retention; the assessment's
4-vCPU/8-GB host suggestion and published prices are hypotheses to recheck,
not a purchasing or capacity guarantee.

### TD-18 — Production forward-proxy selection and egress qualification are incomplete

**Priority/ownership:** P1; Scrapeyard deployment/security. Depends on the
operator's proxy endpoint and intended retailer set; supplies TD-17's egress path.

**Problem:** production Compose enables untrusted submissions, and startup
requires a nonempty, non-`direct` `SCRAPEYARD_PROXY_URL`. The stack supplies
Redis and an egress probe but no filtering forward proxy. The HTTPS ingress
proxy in TD-19 does not satisfy this requirement. A commercial scraping proxy
is not automatically evidence of safe destination filtering.

**Start here:** [egress contract](docs/DEPLOYMENT.md#egress),
[Compose](docker-compose.yml), [deploy wrapper](security/deploy-secure-compose.sh),
[IP policy](src/scrapeyard/engine/ip_policy.py),
[transport authorization](src/scrapeyard/api/transport_policy.py), and
[egress tests](tests/unit/test_egress_policy.py).

**Work:** select the smallest suitable existing proxy/service; document its
address resolution and connected-destination filtering for HTTP and CONNECT,
authentication, availability, and secret delivery. Wire it through the existing
settings and narrowly reviewed host-policy exceptions. Preserve host egress
filtering, startup attestation, and the browser sandbox. Ordinary Eyebox
credentials retain `submit`/`read` only. Supply a repeatable controlled test for
public fetch success, private/loopback/link-local/metadata denial, redirects,
DNS rebinding, and proxy failure; use controlled destinations for security tests.

**Done when:** the production startup path becomes ready with the selected proxy,
fails closed when proxy/attestation prerequisites are absent, and the denial
matrix passes from the real container across the enabled HTTP/browser
transports. Qualify retailer yield and timing through that same proxy under
TD-17 before activation. Record redacted configuration/version evidence and
update the deployment runbook; trusted local/fixture mode cannot close this item.

### TD-19 — Restricted Hetzner HTTPS ingress and Railway credential handoff are incomplete

**Priority/ownership:** P1; Scrapeyard ingress/security. Coordinate Railway's
outbound-address inventory with [Eyebox item G](../eyebox/TECH-DEBT.md#g-railway-service-configuration-and-ordered-releases).

**Problem:** production Compose exposes no host API port, and the deployment
plan does not supply a production reverse-proxy configuration. Railway's
documented static egress now assigns three IPv4 addresses, requires Pro, may
share addresses between customers, and changes addresses on a region move.
The older single-IP handoff wording needs replacement with the actual assigned
set; API credentials remain essential.

**Start here:** [ingress and credentials](docs/DEPLOYMENT.md),
[API routes](src/scrapeyard/api/routes.py),
[monitoring authentication](docs/MONITORING.md),
[Hetzner runbook](../eyebox/docs/hetzner-scrapeyard-deployment-runbook.md),
[Railway outbound contract](https://docs.railway.com/networking/static-outbound-ips),
and [Caddy certificate challenges](https://caddyserver.com/docs/automatic-https).

**Work:** provide a reproducible Caddy/Nginx deployment connected to the private
Compose network or a loopback-only production binding. Allow the adapter's
`POST /scrape`, `GET /jobs/{id}`, and `GET /results/{id}` methods/paths and
necessary query parameters. Preserve API/idempotency headers and bound request
and transfer limits using TD-17. Apply the full verified Railway address set
at the network boundary, with a named credential restricted to project `eyebox`
and `submit`/`read`. Keep readiness/metrics on a private monitoring path with
separate `health-detail` credentials. Choose DNS-01 or a deliberately reachable
HTTP-01 challenge path so restrictions do not break certificate renewal.
Document secret rotation and address-set changes without printing secrets.

**Done when:** deployed ingest can submit/poll/download, and disallowed sources,
wrong credentials/projects, administrative paths, Redis, Docker, and internal
monitoring endpoints are denied externally. Verify idempotency survives the
proxy, oversized requests fail predictably, logs omit secrets, and certificate
issuance/renewal succeeds under the final firewall policy. Update both handoff
runbooks and record evidence in Eyebox checklist sections 15-17.

### TD-20 — Secure startup after host/Docker restart and runtime monitoring are unqualified

**Priority/ownership:** P1; Scrapeyard host supervision. Coordinate with TD-19's
ingress and TD-22's release artifact selection.

**Problem:** `security/deploy-secure-compose.sh` installs policy during a deploy,
but no checked-in installed boot service restores it after host/Docker restart.
`restart: unless-stopped` neither orders policy restoration nor establishes
authenticated readiness. The current container healthcheck is process liveness.

**Start here:** [deploy wrapper](security/deploy-secure-compose.sh),
[egress installer](security/install-docker-egress-policy.sh),
[AppArmor installer](security/install-chromium-apparmor-profile.sh),
[runtime health](src/scrapeyard/main.py), [monitoring](docs/MONITORING.md), and
[installer tests](tests/unit/test_egress_policy_installer.py).

**Work:** add minimal host supervision, normally systemd plus existing Compose
scripts, that discovers the current bridge, starts required peers, installs
AppArmor and the compatible `DOCKER-USER` policy, and admits work only after
startup attestation/readiness. Handle Docker restart/network recreation and
bound failed-start retries. Retain one application process, non-root execution,
seccomp, capabilities, and filesystem limits. Wire private authenticated
readiness/metrics collection and alerts for failed background work, queue age,
browser failure/saturation, disk/inodes, and host/container restart loops. Reuse
the documented metric names and the chosen monitoring service; coordinate
backup-age alerts with TD-21 and Eyebox freshness alerts with item E.

**Done when:** a cold boot, Docker restart, and Compose network recreation
restore the intended controls and exactly one instance automatically. Inject
policy/probe/Redis failure and show no unprotected work starts and retry behavior
is bounded. Verify an actual readiness/queue/disk alert and a no-data condition
reach the operator and recover. Retain redacted effective-policy, resource,
readiness, and reboot evidence; a plain `docker compose up` is insufficient.

### TD-21 — Unattended coordinated off-host backup and restoration are incomplete

**Priority/ownership:** P1; Scrapeyard recovery. Eyebox owns its PostgreSQL
recovery in [item I](../eyebox/TECH-DEBT.md#i-railway-postgresql-recovery-and-scheduled-maintenance).

**Problem:** the repository has a tested quiesced backup-set helper, but no
complete unattended production backup/upload/retention/alert installation.
The helper excludes Redis and encryption key material. Independent snapshots
of live SQLite files, artifacts, and Redis are not a coordinated recovery set.

**Start here:** [backup helper](scripts/qualification_backup.py),
[required quiescing order](docs/TESTING.md#quiesced-backup-and-fresh-restore),
[encryption recovery](docs/SECRET_STORAGE.md#rotation-backup-restore-and-key-loss),
and [release qualification](scripts/run_release_qualification.sh).

**Work:** wrap the existing helper in a scheduled operation: pause ingress/new
dispatch, quiesce application writers, follow the documented Redis persistence
steps, capture all three databases and result/adaptive files, validate the
manifest, encrypt and upload off-host, then resume safely. Define retention,
failure cleanup, disk headroom, backup age, and operator notification. Store
keyring recovery material separately with tested access. State whether queued
work is preserved or recovered/reconciled and package the corresponding Redis
state explicitly. Hetzner server backups are supplemental; attached provider
Volumes are excluded, while Docker volumes on the root disk belong to that disk.

**Done when:** an unattended scheduled backup succeeds, upload/storage failure
and stale-backup alerts reach the operator, and the service recovers safely from
an interrupted backup. Restore off-host data, keys, and the chosen Redis recovery
state into an isolated deployment. Verify known successful/failed jobs, readable
results, decrypted configuration, and queued-work recovery without unintended
external submissions or webhooks. Record measured RPO/RTO, object checksums,
retention, and an operator-executable recovery procedure. A manifest-only check
or provider disk snapshot does not close this item.

### TD-22 — Deployment and rollback lack retained, qualified release artifacts

**Priority/ownership:** P2; Scrapeyard release delivery. Integrates with TD-20;
coordinate joint release IDs with Eyebox item G.

**Implementation update (2026-09-07):**
[`scripts/release_artifact.py`](scripts/release_artifact.py) now builds a
committed revision, gates publication on the existing scan/quick qualification,
and retains source/security files, reports, and all three runtime images in a
checksummed bundle. The secure wrapper's `--release DIRECTORY` path verifies
and loads it before stopping the app, preserves policy/attestation ordering,
and disables builds/pulls. The [rollback procedure](docs/DEPLOYMENT.md#retained-release-artifacts-and-rollback)
covers previous-release retention, key/backup prerequisites and schema
compatibility. Local validation: Ruff and mypy passed; the full suite passed
2,084 tests with 16 live Redis skips (88.51% coverage), and the expanded release,
wrapper and CI checks passed 22 tests with warnings treated as errors.
Artifact creation was attempted locally and correctly refused before building
because `scrapeyard_backend` already owns the qualification lane's fixed
`172.30.0.0/24` subnet. No qualified candidate was published and no running
deployment was changed. **Still open:** prepare the current and previous
artifacts on an isolated Docker host, then complete the deployed acceptance
checks below and record their evidence before removing this entry.

**Problem:** the secure wrapper rebuilds Scrapeyard from the checkout on every
deployment. CI builds/scans images, but the checked-in workflow does not provide
a retained production image and tested rollback path independent of a new
dependency/browser download. Rebuilding an older revision is not proof that
the previously qualified artifact is available during an incident.

**Start here:** [Dockerfile](Dockerfile),
[container-security workflow](.github/workflows/container-security.yml),
[security scan](scripts/run_container_security_scan.sh),
[deploy wrapper](security/deploy-secure-compose.sh), and
[qualification runner](scripts/run_release_qualification.sh).

**Work:** retain the built/scanned/qualified image by immutable digest or verified
archive, plus its source revision and matching Compose/security configuration.
Add the smallest explicit promotion/rollback path that consumes that artifact
without rebuilding and preserves stop/dependency/policy/attestation ordering.
Keep a previous known-good artifact and enough local disk to restore it. Record
the pre-release backup/key requirements and SQLite migration compatibility;
choose data restore or forward repair when an older binary cannot use the new
schema. Preserve the existing local build workflow where useful.

**Done when:** deploy the retained candidate, reject a failed qualification,
then restore the previous compatible release without build-network access.
Verify readiness, known result reads, effective security policy, and one
application instance after both transitions. Exercise the documented data
restore branch in isolation if schema rollback requires it; record image/config
identifiers and commands so another operator can repeat the rollback.

### TD-23 — The browser URL guard pays a thread hop and a DNS lookup on every subrequest

**Priority/ownership:** P3; browser fetch efficiency. Measured locally; the
guard itself must not change without a security review.

**Problem:** `_guarded_async_intercept_route` runs `assert_public_url` through
`run_thread_work` for every browser subrequest, and each call resolves the host
again. Measured over 6,500 subrequests on 40 hosts: the lexical checks alone
take 12 us per request, the thread hop brings it to 126 us wall and 347 us CPU
(about 0.8 s wall and 2.3 s CPU per run), and an uncached resolver answer costs
up to 50 ms, which dominates. With four run threads, slow lookups also delay page
loads: a fixture page of 250 subrequests on unresolvable `.test` names spends
most of its 5.5 s in guard lookups.

**Start here:** [browser_debug.py](src/scrapeyard/engine/browser_debug.py)
(`_guarded_async_intercept_route`),
[url_guard.py](src/scrapeyard/engine/url_guard.py) (`assert_public_url`),
[run_threads.py](src/scrapeyard/common/run_threads.py).

**Work:** evaluate a per-run verdict cache keyed by canonical hostname with a
short TTL (for example 30 to 60 seconds): run the lexical checks inline on every
request, and hop to a thread and resolve only when the host has no fresh
verdict. Keep rejections uncached or cached as rejections only, keep
`URLResolutionError` behavior when resolution is required (proxy or remote
CDP), and write down the rebinding argument (the browser resolves on its own
after the guard in both designs). Measure on real traffic first: if resolver
answers are already cached upstream and fast, the thread hop alone may not be
worth a guard change.

**Done when:** a security review accepts the design, the guard's existing tests
pass unchanged, and a before/after run on the same pages shows the saved CPU and
wall time with identical records and traffic counts.

### TD-24 — Browser reuse and its script cache are tuned on fixtures, not real traffic

**Priority/ownership:** P3; browser traffic and memory. `execution.reuse_browser`
is opt-in; the numbers behind its defaults come from local fixtures only.

**Problem:** three choices rest on fixture measurements: the shared-browser
replacement threshold (`RECYCLE_AFTER_REQUESTS = 250` routed requests, chosen to
keep peak memory within about 5% of one browser per target), the script cache
bounds (32 MiB per run, 8 MiB per response), and the cache admitting only
responses with explicit freshness and no `Vary` beyond `Accept-Encoding`. On
fixtures, reuse cut requests by 60-66% and bytes by 82-95% with identical
records, and peak memory rose 5-8% in-process and 12-15% in the production
image with 1 MB pages (driver heap timing). Sites that send `no-cache` with
`ETag`, `Vary: Origin`, or cache-busting query strings get no benefit, and heavy
pages reach the threshold within one target, so they still launch one browser
per target.

**Start here:** [browser_pool.py](src/scrapeyard/engine/browser_pool.py),
[asset_cache.py](src/scrapeyard/engine/asset_cache.py),
[browser_session.py](src/scrapeyard/engine/browser_session.py) (`fetch`,
`_ReleasedRoute`), and `run_budget.traffic` (`cached`, `resource_types`).

**Work:** once runs with `reuse_browser` exist, compare each against the same
job without it: `traffic.requests`, `traffic.bytes`, `traffic.cached`, the
`script` share of `resource_types`, launches, wall time, and container anon
peak. Only if real pages show a large uncached script share, consider
revalidation (answer a `304` from the stored body) or `Vary: Origin` support.
Retune the replacement threshold from real peaks, or count fulfilled bytes as
well as requests if cached bodies drive the driver's heap.

**Done when:** the defaults are confirmed or changed with a before/after
measurement on real runs, with identical records, and README states the
measured effect instead of fixture numbers.
