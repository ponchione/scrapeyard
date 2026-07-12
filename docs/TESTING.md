# Testing

## Checked-in continuous integration

GitHub Actions runs the checked-in workflows for every pull request and every
push to `main`. They need only repository read access; they do not use
production credentials, deployment access, or public scrape targets.

The `CI` workflow exposes these checks:

- `Quality / Python 3.10` and `Quality / Python 3.12` install the lock file,
  validate it, run Ruff and mypy over their configured scopes, and run the full
  pytest suite with branch coverage and the 80% coverage floor.
- `Distribution build / Python 3.12` builds both the wheel and sdist and then
  verifies that each archive contains exactly the checked-in SQL migrations.
- `Live Redis / Python 3.12` runs only the `live_redis` tests against an
  isolated Redis 7 service and the real arq queue/worker implementation.

The separate `Container and browser smoke` workflow adds the production-image
lane owned by Audit Item 14. Its `Production image / real browsers` job runs on
manual dispatch and on pull requests that change the Docker/Compose build,
lock file, smoke harness, or runtime API/config/browser/queue/storage paths.
This intentionally resource-intensive lane does not replace or weaken any of
the always-on CI, live-Redis, distribution, or dependency-audit gates.

The separate `Dependency audit` workflow exposes `Python 3.10` and `Python
3.12` checks. Each audits the pinned packaging tools and both the locked
development environment and exported production-only graph. It runs on every
pull request and push to `main`, can be dispatched manually, and retains the
weekly Monday schedule introduced by Audit Item 11. The CI workflow does not
duplicate those audit jobs.

The minimum supported interpreter is Python 3.10. Python 3.12 matches the
Docker production runtime. The complete static and default test gates run on
both. Distribution and live-service behavior run on the production version to
keep those focused lanes bounded.

From an environment with Poetry 2.3.4 and `poetry-plugin-export` 1.10.0, the
local equivalents of the CI gates are:

```bash
poetry check --lock
poetry sync --no-interaction
poetry run ruff check src tests
poetry run mypy src
poetry run pytest
rm -rf dist
poetry build
poetry run python scripts/inspect_distribution.py dist
poetry run python scripts/smoke_install_distribution.py dist
poetry run python scripts/audit_dependencies.py all
./scripts/run_live_redis_tests.sh
```

## Packaging metadata and toolchain

The supported packaging toolchain is pip 26.1.2, Poetry 2.3.4, and
`poetry-plugin-export` 1.10.0. Install the same versions in an isolated Poetry
tool environment and verify them before changing the lock or building a
release:

```bash
pipx install --force poetry==2.3.4
pipx inject --force poetry poetry-plugin-export==1.10.0
poetry run python scripts/check_packaging_toolchain.py
```

CI and the Docker builder install those exact pins and run the same checker;
version drift fails before dependency resolution or export. `pyproject.toml`'s
PEP 621 `[project].version` is the sole static application version. At runtime,
`scrapeyard.__version__` reads the installed distribution metadata, so the
FastAPI application, wheel, sdist, and package metadata cannot carry separate
hard-coded versions.

After any dependency or metadata change, regenerate and validate with the
pinned toolchain:

```bash
poetry lock
poetry check --lock
```

Build and inspect both release archives, then install the wheel into a fresh
temporary virtual environment:

```bash
rm -rf dist
poetry build
python scripts/inspect_distribution.py dist
python scripts/smoke_install_distribution.py dist
```

The inspector requires exactly one wheel and one sdist, the complete
`scrapeyard` package, matching PEP 621/distribution versions, and every
checked-in SQL migration in both archives. The smoke installer uses
`--no-deps` so it validates the wheel layout and metadata without masking a
packaging defect with the editable checkout.

## Single-instance runtime checks

The focused lock and process-configuration contract runs with:

```bash
poetry run pytest --no-cov tests/unit/test_instance_guard.py tests/unit/test_main.py
```

Those tests cover acquisition, active contention, a stale unlocked file,
kernel release after an abrupt child exit, lifespan shutdown release, and
common Uvicorn/Gunicorn worker settings. For a deployment-level check, start
one process against a dedicated Redis database and temporary set of all four
state directories, wait for `/health/live`, then launch a second process with
the identical environment on another HTTP port. The second must exit before
database initialization with `Another Scrapeyard application process owns`.
After both graceful termination and `SIGKILL` of the owner, a replacement must
reach liveness using the same identity. Never run this check against production
state. See [SCALING.md](SCALING.md) for the supported topology and state map.

Run the install, lint, type, and full-suite commands once under Python 3.10 and
once under Python 3.12 when reproducing the complete matrix locally. The live
Redis script is the local equivalent of the service lane: it enables API-key
authentication, checks configurable host port `56379`, starts a dedicated
Compose project, uses Redis database 15, and always removes its containers,
network, and named test volume. In Actions, the runner supplies a fresh Redis
service container on port 6379; fixtures use a unique queue name, flush database
15 before and after each app fixture, and send the configured non-production
API key on protected requests. Both forms retain pytest timeouts and use
`--no-cov` because this focused lane is not the coverage gate.

## Static typing contract

The mypy configuration is strict for every first-party production module. Run
the acceptance command directly with:

```bash
poetry run mypy src/scrapeyard
```

CI runs `poetry run mypy src`, which includes the same complete package rather
than a hand-picked module list. Internal queue, scheduler, webhook, runtime,
API, and storage transitions use concrete settings, config, state, result, and
store types. Cross-component callbacks and health/store dependencies are
expressed as protocols so an untyped dependency result cannot silently become
a job or run state.

The remaining dynamic types are deliberate serialization or adapter
boundaries:

- Scrapling fetchers, pages, DOM elements, and browser callbacks are isolated
  in the engine's fetch, selector, detection, and browser-debug adapters.
- arq's worker constructor, Redis Lua return values, serialized context, and
  result handles are converted or cast inside `queue/pool.py`; the first-party
  worker callback and health surface are typed protocols.
- APScheduler and aiosqlite objects stay inside the scheduler and database
  adapters. First-party scheduler state and storage protocols do not expose
  those library objects.
- YAML/JSON extraction records, debug diagnostics, webhook bodies, and stored
  result payloads remain `Any`-valued mappings because their values are
  intentionally user-defined. Their surrounding config, ownership, status,
  delivery, and persistence metadata is statically typed.

The missing-stub override is limited to Scrapling, arq, APScheduler, and
aiosqlite. It does not relax strictness for first-party modules. Local
`type: ignore` comments are permitted only at a named dependency boundary and
must explain why the dependency's published typing cannot express the call.

Only immutable dependency-download caches (`~/.cache/pip` and Poetry's
content-addressed `~/.cache/pypoetry/artifacts` directory) are shared. CI does
not cache Poetry's adjacent `virtualenvs` directory, SQLite databases, browser
profiles, `/data` directories, credentials, Redis state, or test output.
Failed quality jobs retain JUnit, XML coverage, and HTML coverage diagnostics
when available. A failed Redis job retains its JUnit file and the isolated
service log. Successful distribution archives are retained for seven days; no
workflow publishes them.

Recommended branch-protection checks are all of the following:

- `CI / Quality / Python 3.10`
- `CI / Quality / Python 3.12`
- `CI / Distribution build / Python 3.12`
- `CI / Live Redis / Python 3.12`
- `Dependency audit / Python 3.10`
- `Dependency audit / Python 3.12`

For pull requests matched by its path filter, also require `Container and
browser smoke / Production image / real browsers`. Dispatch that workflow as a
pre-release check when a release candidate did not naturally match those
paths.

The workflows use read-only repository permissions, cancel superseded runs for
the same pull request or ref, continue independent matrix entries after one
fails, and apply bounded job timeouts. The container/browser lane has a
60-minute job timeout because its cold build downloads all three real browser
runtimes. It does not use a Docker layer cache in Actions and does not cache
runtime state.

## Production container and browser smoke lane

Run the complete lane locally from the repository root with:

```bash
./scripts/run_container_browser_smoke.sh --no-cache
```

That is the cold-cache verification command. Repeat it without the flag to
prove warm-cache build and runtime repeatability:

```bash
./scripts/run_container_browser_smoke.sh
```

The runner requires a working Docker daemon, Docker Compose v2, Bash,
`python3`, `curl`, GNU-compatible `timeout`, `mktemp`, and `od`. For its
deployment-bridge-and-source-scoped `DOCKER-USER` egress proof it uses root,
passwordless sudo, or a
short-lived digest-pinned host-network helper container with `NET_ADMIN`. It fails
early with a focused prerequisite or port-conflict message. The host must
support AppArmor and unprivileged user namespaces; the runner temporarily loads
the repository's restrictive Chromium profile when it is not already present.
It uses the pinned Playwright seccomp policy rather than disabling filtering.
Docker Desktop must have enough Linux VM resources and support the
`linux/amd64` image. Camoufox may emit its upstream virtual-display or
fingerprint warnings; warnings do not pass the lane in place of successful
extraction.

The default topology is:

- Scrapeyard at `http://127.0.0.1:18420`, authenticated with a freshly
  generated, mode-0600 API key that is never placed in diagnostics;
- fixture control at `http://127.0.0.1:18080`;
- Redis 7 on an unexposed backend network and persistent named volume;
- the production Scrapeyard image, its normal `/data` named volume, direct
  UID/GID 10001 startup, read-only root filesystem, dropped capabilities,
  bounded resources, no-new-privileges, a `SYS_CHROOT`-only bounding set, and
  the Chromium-specific seccomp/AppArmor profiles;
- a fixture-only internal network using `203.66.81.240/28` as an emulated
  public range and a second internal `10.77.14.0/29` network for deliberately
  unsafe destinations. Neither fixture data network provides an Internet
  route. A separate fixture-control bridge exists only to support its
  loopback-published observation endpoint; Scrapeyard is not attached to it.

Override non-conflicting ports or the project name with
`SCRAPEYARD_SMOKE_API_PORT`, `SCRAPEYARD_SMOKE_FIXTURE_PORT`, and
`SCRAPEYARD_SMOKE_PROJECT`. The deployment Compose mapping itself also accepts
`SCRAPEYARD_BIND_ADDRESS` and `SCRAPEYARD_PORT` in the explicit local override;
its bind default is `127.0.0.1`. Build, health, and per-job deadlines are configurable through
`SCRAPEYARD_SMOKE_BUILD_TIMEOUT`, `SCRAPEYARD_SMOKE_START_TIMEOUT`, and
`SCRAPEYARD_SMOKE_JOB_TIMEOUT`, with defaults of 2,400, 180, and 120 seconds.

The fixture and assertions cover static HTML and a safe redirect for `basic`;
JavaScript insertion, consent click, scrolling, and two load-more clicks for
standard `dynamic`, `dynamic` with `browser.stealth: true`, and `stealthy`;
and screenshot creation for every successful browser-backed run. Each browser
mode must extract exactly `javascript-ok`, `consent-ok`, `scroll-ok`,
`load-more-ok`, and loaded count `2`; basic must extract `static-ok` and
`safe-redirect-ok`. Merely launching a browser is not sufficient.

SSRF checks include an API-rejected direct private target, a dynamic browser
subrequest to `fixture.private.test`, and both basic and browser redirects to a
private destination. Scrapeyard resolves the ordinary fixture hostname to an
emulated global address, so the production URL guard remains unchanged. The
private fixture endpoint is not attached to Scrapeyard's networks. The lane
installs the checked-in host egress chain and proves direct private and metadata
requests fail below the application. It also requires the intercepted subrequest to
produce a classified browser request failure, both unsafe redirect targets to
fail, and the fixture's protected-endpoint counter to remain exactly zero.

The fresh named volume inherits build-time UID/GID 10001; startup performs no
root ownership repair. In-container checks verify PID 1's UID, zero effective
capabilities, seccomp filtering, a read-only root filesystem, runtime-user
ownership/write access for `/data` and tmpfs paths, immutable browser caches,
locked Playwright and rebrowser package versions and Chromium revisions, the
unprivileged Chromium user-namespace sandbox, the installed Camoufox
asset/version, result ownership, and PNG contents. The runner copies
only the `item14-smoke` result subtree to a temporary host directory to prove
screenshots are retrievable, then removes that copy.

Plan for 4 CPU cores, 8 GiB RAM, and 15 GiB free disk; 2 cores and 6 GiB are a
practical lower bound but take longer. Network access is needed only to pull
base images and, during an uncached build, locked Python packages and upstream
browser assets. A cold run is expected to take 12–30 minutes and download/build
several gigabytes. A warm run usually takes 2–8 minutes because immutable image
layers and browser downloads are reused. The running services are configured
for at most one browser and two concurrent jobs; typical steady memory is
under 2 GiB, with short browser/build peaks that can be higher. Actual build,
total duration, final container stats, and image bytes are written to the
focused diagnostic directory for each run.

On the July 12, 2026 reference run (x86-64 Linux, 125 GiB host RAM, warm base
images, fast local/network storage), the scoped `--no-cache` build took 164
seconds and the complete cold lane took 196 seconds. Repeated warm builds took
1–3 seconds and their complete lanes took 35–36 seconds. The image was
3,991,984,747 bytes. The final idle sample was 162.9 MiB for Scrapeyard, 5 MiB
for Redis, and 13.6 MiB for the fixture; browser and build peaks are higher and
were not sampled as a soak/load measurement. These observations are not
performance gates and do not absorb Item 15's recovery/load/soak scope.

On success, the runner requests a timed graceful stop, requires Uvicorn's
shutdown marker and accepts exit code zero or Docker init's signal-forwarded
143, then removes only its Compose project's
containers, four networks, and two named volumes. On failure or interruption,
an EXIT/signal trap first retains bounded service/fixture logs, Compose and
health output, fixture protected counts, relevant API submission/result/error
payloads, and only the `item14-smoke` result/screenshots, then performs the same
scoped cleanup. Credentials, browser profiles, SQLite databases, whole `/data`
trees, Docker caches, and unrelated project resources are never archived or
removed. Diagnostics default to `artifacts/container-browser-smoke`; override
with `SCRAPEYARD_SMOKE_DIAGNOSTICS_DIR`. The workflow uploads that directory
for seven days only when the lane fails.

Run the fast checks before merging changes:

```bash
poetry run ruff check src tests
poetry run pytest
```

For queue behavior against a real Redis instance, run:

```bash
./scripts/run_live_redis_tests.sh
```

The live Redis runner starts an isolated Redis container on host port `56379`,
runs the `live_redis` pytest marker, and tears the container down. Normal
`poetry run pytest` executions skip those tests when the isolated Redis
instance is not available.

Useful targeted lanes:

```bash
poetry run pytest tests/unit
poetry run pytest --no-cov tests/integration
poetry run pytest --no-cov tests/live_redis
```

The integration tests monkeypatch the worker pool so jobs still exercise the
queue-facing app contract without requiring Redis for the default test suite.
Focused integration and live-service lanes disable the repository-wide coverage
floor because they intentionally execute only a subset of production modules;
the full `poetry run pytest` gate remains responsible for enforcing 80% branch
coverage.

## Recovery, restore, load, and soak release qualification

Audit Item 15 adds a separate destructive qualification lane. It runs the
unchanged production Dockerfile/runtime class with real Redis 7 AOF, real
Playwright Chromium, the Item 14 fixture and browser/security networks, the
normal non-root UID/GID 10001 runtime, the production `/data` volume, default
Chromium seccomp/AppArmor policies, read-only root filesystem, and the same
capability boundary. It does not contact public scrape targets or webhook
services. The fixture's emulated-public and private networks remain isolated,
and Item 14's browser-route/connected-IP rejection and protected-endpoint
SSRF proof are unchanged.

Run the complete bounded profile locally:

```bash
./scripts/run_release_qualification.sh --profile quick
```

Run the longer pre-release profile, normally on the intended host class:

```bash
./scripts/run_release_qualification.sh --profile full --no-cache
```

Use `--no-build` only after the Compose project image already exists. For
focused diagnosis, `--phase recovery`, `redis_restart`, `load`, `soak`, or
`backup_restore` runs one phase with the same traps and cleanup. The full
profile repeats every phase; it is not a substitute for a successful complete
quick run. `./scripts/run_release_qualification.sh --help` lists every port,
duration, resource, and threshold override.

The `Recovery, restore, load, and soak qualification` workflow has two jobs:

- `Quick recovery and restore / production runtime` runs the complete quick
  profile with a no-cache image build on relevant pull-request paths or a
  manual `profile=quick` dispatch.
- `Full load and soak / production runtime` runs the full profile on a manual
  `profile=full` dispatch and at 07:41 UTC each Saturday.

Both jobs have read-only repository permission, SHA-pinned checkout/upload
actions, per-ref concurrency, PR-only cancellation, 60/120-minute timeouts,
and no caches. They upload only the bounded qualification report/log set for
seven days. They never cache or upload databases, Redis files, `/data`,
backups, generated credentials, or browser profiles. The Item 11–14 workflows
and required checks remain separate and unchanged.

### Crash boundaries and convergence

The destructive coordination hook is disabled by default. It has no HTTP or
Redis trigger. A checkpoint is active only when the container has both
`SCRAPEYARD_QUALIFICATION_MODE=true`, an exact
`SCRAPEYARD_QUALIFICATION_CRASH_POINT`, and the runner-created sentinel in a
UID-1000-only tmpfs. Misconfiguration without qualification mode is rejected
during settings validation; a missing or invalid sentinel raises rather than
silently enabling a crash. At the marker, the process blocks and the host
runner delivers SIGKILL, so termination is externally observable.

Each crash point uses isolated Item 15 Redis/data volumes. This is necessary
because arq intentionally retains a killed delivery's admitted base-queue
member and in-progress lease until its timeout; accumulating four independent
kills in one base queue would head-of-line block the fifth probe. The runner
asserts convergence before removing only that scenario's Compose volumes.

| Crash point | Persisted boundary | Required result after restart/reconciliation |
| --- | --- | --- |
| `after_enqueue_before_claim` | SQLite queued job and Redis priority payload exist; no claim | The same run is redelivered once and completes with its result. |
| `after_claim_run_creation` | Parent and `job_runs` row are running | After the six-second qualification lease expires, startup fails the stale run and reconciles its failed webhook intent. |
| `during_target_execution` | Run owns execution but no terminal state | Startup fails the stale run; no second run row or concurrent owner appears. |
| `after_result_artifact_write` | Atomic result file replacement completed; metadata insert has not | Startup fails the stale run; cleanup removes the unindexed orphan run directory after its one-second qualification grace; `/results` returns 404. |
| `during_run_finalization` | Result file/metadata exist; terminal transaction has not started | Startup fails the stale run. The indexed result remains recoverable and queryable while job/run truth is terminal failed. |
| `during_webhook_intent_transaction` | Run/job updates and outbox insert are uncommitted in one `BEGIN IMMEDIATE` transaction | SQLite rolls the transaction back. Startup fails the stale owner, terminal reconciliation creates one durable failed intent, and delivery converges. |
| `after_terminal_state_before_delivery_ack` | Run, parent, result metadata, and durable terminal intent committed; arq result/HTTP delivery not acknowledged | Job/result remain complete and startup outbox replay produces exactly one fixture delivery. |

Recovery assertions include job and run status, run count, result HTTP status,
webhook attempt count, queue depths, and elapsed recovery. The qualification
lease is deliberately 6 seconds with a 2-second heartbeat; deployment defaults
remain 600 and 30 seconds. arq's job timeout is explicitly derived from
`SCRAPEYARD_RUN_MAX_DURATION_SECONDS +
SCRAPEYARD_WORKERS_CANCELLATION_GRACE_SECONDS`, so the Redis in-progress lease
does not contradict the service's configured run budget.

### Quiesced backup and fresh restore

`scripts/qualification_backup.py` implements the backup-set contract. The
required snapshot order is:

1. Stop API ingress and APScheduler.
2. Drain/stop workers and the webhook dispatcher within the shutdown grace.
3. Close all SQLite connections and allow WAL state to checkpoint.
4. Run Redis `WAITAOF 1 1 5000` and `SAVE` for the separately persisted queue
   volume. Redis state is not part of the local `/data` backup set.
5. Use SQLite's backup API on `/data/db/jobs.db`, then `errors.db`, then
   `results_meta.db`.
6. Copy `/data/results` and `/data/adaptive` while the service remains stopped.
7. Write and validate `manifest.json`, then expose the completed set.

The manifest format is `scrapeyard-backup-v1`. It records this exact order,
the three required database names, and every payload file's relative path,
byte count, and SHA-256. Logs and Redis persistence are excluded. Validation
rejects a missing/extra file, duplicate or unsafe path, checksum/size drift,
missing database/table, failed `PRAGMA integrity_check`, a result/error without
its job/run, and result metadata without `results.json`. Read-only immutable
validation prevents WAL/SHM side effects from becoming undeclared files.

Restore accepts only a truly empty target or the four empty mount points that
Docker initializes from the production image (`db`, `results`, `adaptive`, and
`logs`). It stages on the destination volume, restores `db`, `results`, and
`adaptive`, validates again, and refuses any unknown/non-empty target. The
qualification runner deletes the original named volumes, creates a fresh
deployment and Redis volume, restores the temporary set, and starts the normal
image. It compares exact pre/post API payloads for a known successful job/run
and result, a known failed job/run and its error rows, a disabled schedule's
persisted metadata, a durable webhook-outbox row, plus exact controlled
adaptive metadata bytes. The temporary backup is never a diagnostic artifact
and is deleted by success, failure, and signal traps.

Manual operational form, after performing the same quiescing order, is:

```bash
python scripts/qualification_backup.py create \
  --data-root /data --output /secure-temporary/scrapeyard-set --quiesced
python scripts/qualification_backup.py validate \
  --backup /secure-temporary/scrapeyard-set
python scripts/qualification_backup.py restore \
  --backup /secure-temporary/scrapeyard-set --data-root /fresh/data
```

Do not place the temporary set under `artifacts/`, and do not restore over a
running or populated deployment.

### Redis restart and persistence assumptions

The production Compose service and qualification overlay use Redis AOF. The
qualification policy is explicit: `appendonly yes`, `appendfsync everysec`,
and `aof-use-rdb-preamble yes`, backed by the project-scoped `redis-data`
volume. The scenario saturates four running jobs, leaves six high/normal/low
priority jobs queued, waits for local AOF acknowledgement, SIGKILLs Redis,
keeps it unavailable beyond the six-second run-heartbeat lease, and requires
the protected `/health/ready` endpoint to report unhealthy.

After Redis returns, restart the single Scrapeyard process. This is required:
a prolonged disconnect terminates arq's worker control loop even though the
surviving API process can reconnect for a later health probe. Startup then
fails stale SQLite owners, clears their now-unlocked base deliveries, and
drains the durable priority backlog. The lane requires all jobs terminal, one
run row per job, no simultaneous duplicate owner, all priority depths zero,
Redis recovery under 30 seconds, and total drain under 180 seconds. AOF's
one-second policy still has its documented loss window unless callers use
`WAITAOF`; the runner does so before its destructive restart.

The final focused Redis rerun completed in 73.4 seconds: service recovery was
18.224 seconds, drain was 59.699 seconds, all ten jobs completed with one run
each, the fixture observed exactly ten target requests (no duplicate fetch),
and all three priority depths ended at zero.

### Controlled load and soak profiles

Load uses only the local fixture and proves these behaviors:

- 12 concurrent delayed basic jobs cannot exceed 4 active workers;
- four concurrent targets spanning stock Playwright, rebrowser stealth, and
  Camoufox cannot exceed 2 active browsers;
- a 990-record result near the 1,000-record cap and a separate 350-record
  result near the 2-MiB qualification output budget both succeed;
- 1,001 records, a result over 2 MiB, a response over 8 MiB, and work over the
  30-second run duration each persist a classified failed outcome;
- a body over 65,536 bytes returns 413 before auth/router/queue work;
- 80 concurrent authenticated administrative reads remain 200 during an
  eight-job queue burst, while a missing API key remains 401;
- mixed priority backlog is observed and completely drained.

The quick soak runs for 130 seconds. The full pre-release soak runs for 900
seconds by default; increase `SCRAPEYARD_QUALIFICATION_SOAK_SECONDS` up to
3,600 for a release-specific longer hold. It exercises a real once-per-minute
APScheduler job, one webhook that fails twice then delivers on attempt three,
one webhook that reaches the three-attempt dead-letter state, five-second
cleanup passes, and an aged orphan. It samples RSS/CPU, DB/result/adaptive
bytes and file counts, `/proc` task/fd counts, SQLite fd count, TCP connection
count, queue/worker/browser state, and request latency every five seconds.
The final calculation checks scheduler count, exact webhook attempt/delivery
counts, orphan removal, and bounded database, artifact, connection, task, and
memory growth. This is release-test instrumentation, not a production metrics
or readiness system.

### Intended host and pass/fail thresholds

The baseline is the same practical target as Item 14: 4 CPU cores, 8 GiB RAM,
and 15 GiB free local disk. Compose constrains Scrapeyard to 4 CPUs/8 GiB, so
thresholds do not scale to the much larger development host. A 25% CPU
sampling tolerance permits short accounting jitter above the four-core cap.

| Measurement | Default failure threshold |
| --- | ---: |
| Authenticated request latency | p95 > 750 ms |
| API/Redis/process recovery | > 30 s |
| Redis mixed-backlog drain | > 180 s |
| Scrapeyard RSS peak | > 6,144 MiB |
| Soak RSS growth | > 512 MiB |
| Load DB/result/adaptive growth | > 1,024 MiB |
| Soak SQLite DB growth | > 64 MiB |
| OS task/thread growth | > 4 |
| CPU sample | > 400% plus 25% tolerance |
| Worker/browser concurrency | > configured 4/2 |
| Record/output/fetch/duration/request limits | Any bypass or non-queryable failure |

Override thresholds with the `SCRAPEYARD_QUALIFICATION_*` variables printed
by `--help`; invalid/non-numeric settings fail before Docker state is created.
A threshold failure reports its phase, observed value, configured threshold,
and recovery action. The quick global/phase timeouts are 1,800/300 seconds;
full defaults are 5,400/300. Build timeout is 2,400 seconds. Expected running
network traffic is loopback API/fixture control plus internal Docker networks;
only image pulls and uncached dependency/browser builds need external network.

On the July 12, 2026 local x86-64 reference host, the complete warm-build quick
profile took 614.2 seconds in the driver and 636 seconds end to end. Crash
recoveries were 10.609–12.026 seconds; Redis recovery/drain were 18.268/59.745
seconds; load p50/p95 were 1.936/18.168 ms; peak process samples were 459.9
MiB and 171.03% CPU. The 130-second soak fired twice, retained two results,
grew RSS 18.2 MiB and SQLite 560,320 bytes, added four artifact files, and had
no SQLite-fd growth. Its 2,478,055-byte manifest contained 53 files and exactly
restored 45 jobs, 45 runs/results, eight errors, three outbox rows, the schedule,
and adaptive metadata.

The complete full profile took 1,373.2 seconds in the driver and 1,391 seconds
end to end. Its 900-second soak fired 15 times, retained exactly four results,
grew RSS 29.3 MiB and SQLite 2,307,200 bytes, added six artifact files, and had
no SQLite-fd growth. Full load p95 was 18.763 ms; peak samples were 461.3 MiB
and 173.68% CPU. Its 2,498,281-byte/55-file backup exactly restored 45 jobs,
58 runs, 47 results, 21 errors, three outbox rows, schedule metadata, and the
adaptive artifact. The final warm Item 15/Item 14 smoke image was
3,992,004,096 bytes (the pre-Item-15 Item 14 baseline was 3,991,984,747).
Machine-readable samples remain in each `qualification-report.json`;
update this paragraph when a materially different release host becomes the
baseline.

After adding the separate 990-record success and mixed Playwright/rebrowser/
Camoufox concurrency cases, the final focused load rerun took 54.2 seconds:
p50/p95 were 1.854/16.399 ms, worker/browser peaks were exactly 4/2, sampled
RSS/CPU peaks were 677.7 MiB/175.57%, and total DB/result/adaptive growth was
5,260,891 bytes. All four hard-limit failures still converged as expected.

### Ports, diagnostics, and cleanup

Defaults are authenticated API `127.0.0.1:19420` and fixture control
`127.0.0.1:19080`; Redis is never host-published. A random mode-0600 API key is
held in a mode-0700 temporary directory and removed on every exit. Override
ports/project with `SCRAPEYARD_QUALIFICATION_API_PORT`,
`SCRAPEYARD_QUALIFICATION_FIXTURE_PORT`, and
`SCRAPEYARD_QUALIFICATION_PROJECT`. Existing listeners or project resources
cause an early failure.

On failure, diagnostics contain at most 500 log lines per service, Compose
state, health and queue-depth state, fixture/webhook counters, Redis INFO text,
the classified phase failure, measurements/report, exact expected/observed API
state when restore differs, and at most four browser screenshots. On success,
the same bounded report supports threshold inspection. A final content scan
rejects the generated key, SQLite content/signatures, `.db`, AOF and RDB files.
No browser profiles, backup payload, restored database, whole `/data`, Docker
cache, or unrelated directory is copied.

EXIT/INT/TERM/HUP traps remove only containers, networks, and named volumes
carrying the selected Compose project label, then delete the temporary backup
and credential directories. The runner verifies no such project resources or
temporary directories remain. It never runs `docker system prune`, builder
prune, or global image/volume/network cleanup. Camoufox virtual-display and
Redis host `vm.overcommit_memory` warnings are upstream/platform warnings;
they do not replace successful browser extraction, AOF reload, or threshold
assertions.

## Dependency vulnerability audits

Install the locked development environment before auditing it:

```bash
poetry sync
poetry run python scripts/audit_dependencies.py development
```

The development audit examines the complete Poetry environment. It skips only
the editable `scrapeyard` package because this local project is not published
on PyPI; that skip is not a vulnerability or an approved exception.

Export and audit the production-only dependency graph with:

```bash
poetry run python scripts/audit_dependencies.py export-production --output /tmp/scrapeyard-requirements.txt
poetry run python scripts/audit_dependencies.py production
```

Run both enforced gates with:

```bash
poetry run python scripts/audit_dependencies.py all
```

Production export requires Poetry's `export` command. The Docker builder and
dependency-audit workflow use the audited combination of Poetry 2.3.4,
`poetry-plugin-export` 1.10.0, and pip 26.1.2. With another Poetry 2.x
installation where the command is absent, install the official export plugin
into Poetry's tool environment. The export is derived only from `poetry.lock`
and the main dependency group; repeated exports from the same lock are
byte-for-byte stable and are the exact input to the production `pip-audit`
lane.

Both audits fail on every known vulnerability unless its exact advisory ID is
listed in `security/dependency-audit-exceptions.json`. The checked-in list is
empty. An exception must be reviewed and contain all of the following fields:

```json
{
  "id": "CVE-YYYY-NNNN",
  "package": "affected-package",
  "scopes": ["production"],
  "owner": "team-or-person",
  "rationale": "Why no compatible fixed version can be used yet.",
  "expires": "YYYY-MM-DD",
  "upstream": "https://upstream.example/issues/123"
}
```

Allowed scopes are `development` and `production`. Expired, duplicate,
incomplete, or malformed entries fail closed before `pip-audit` runs. Remove an
exception as soon as a compatible fix is available; extending an expiry
requires a new review with updated evidence.

### 2026-07-12 refresh record

The pre-refresh audit at commit `74fcb5e` reported 37 advisories across nine
packages. Runtime dependencies accounted for `aiohttp` (11), `idna` (2),
`pydantic-settings` (1), `PyJWT` (8), and `Starlette` (6). Development or local
packaging tooling accounted for `cryptography` (1), `dulwich` (5), `msgpack`
(1), and `pip` (2). `msgpack` and `pip` came from the locked `pip-audit` graph;
the local `cryptography` and `dulwich` installations came from untracked Poetry
tooling and are removed by `poetry sync`.

The refresh raises the direct `pydantic-settings` floor to 2.14.2, locks the
affected packages to their compatible fixed releases, and refreshes the
Docker/CI packaging tools because Poetry 1.8.5 constrained Dulwich below its
fixed line. The final image also upgrades pip to its audited fixed version. No
vulnerability exception is approved. After synchronization, both the complete
development audit and the exported production-only audit report no known
vulnerabilities. The dedicated GitHub Actions workflow runs both audit scopes
weekly and on every pull request and change to `main` for Python 3.10 and the
Docker runtime version, Python 3.12. Keeping it separate from the broader CI
workflow makes the security checks independently requireable without running
duplicate audit jobs.
