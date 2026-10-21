# Changelog

All notable changes to Scrapeyard are documented here.

Format: [Semantic Versioning](https://semver.org/) — `MAJOR.MINOR.PATCH`.
- **MAJOR** — breaking API or config changes (response shapes, removed endpoints, YAML schema changes)
- **MINOR** — new features, new endpoints, new config options (backwards-compatible)
- **PATCH** — bug fixes, performance improvements, internal refactors

Until 1.0, the API is not considered stable and MINOR bumps may include breaking changes.

---

## Unreleased

### Monitoring
- The host check takes `--edge-url`: it asks the public `/health` through
  Cloudflare, Access and the tunnel with a service token of its own, so a
  connector with no connection or an Access policy that refuses the token
  fails the check. The connector's loopback readiness stays unreadable from
  the host.

### Deployment qualification
- Release `e31e6a3` runs on the production host since 2026-10-20, promoted
  over `fbc340c` after a backup. Readiness, the host check, a stored result
  read back byte for byte, a replayed submission and Eyebox's result parser
  passed afterwards.
- Release `fbc340c` ran on the production host from 2026-10-14 (TD-89,
  TD-90, TD-91, TD-20's host check and TD-21's fixes). Its settings name the
  hosts a request may give, which were first read off the host's own
  traffic. After each change the host's checks passed (tunnel included),
  a stored result read back byte for byte and a fixed-key submission
  replayed; a backup and its restore test passed.
- Qualified on the production host on 2026-10-13 and 14 (TD-22, TD-83 and
  TD-84, now closed):
  - A release built, scanned and rehearsed by the release workflow was
    promoted there, rolled back to the release before it and promoted
    again, with readiness, a stored result read back byte for byte, a
    replayed submission, the confinement, the host policy and the tunnel
    checked after each step. Each promotion took 32 to 63 seconds.
  - The seccomp profile derived from Docker's default and the AppArmor
    profile ran there, with browser fetches in all three modes, and the
    confinement check refused `AF_ALG` and `AF_VSOCK` through the 32-bit
    entry and, with the release after it, every attempt TD-87 closed.
  - Docker was upgraded in place to 29.9.0, containerd.io 2.4.1 (runc
    1.5.2) and Compose 5.6.0 with the classic image store kept, and the
    host was rebooted twice, once by unattended upgrades; the boot service
    brought the application back each time, 35 to 39 seconds after boot.

### Added
- `SCRAPEYARD_ALLOWED_HOSTS` (TD-90): when set, a request whose `Host` is not
  one of the names listed gets `400` before anything else reads it, and a
  request from inside the container must name loopback. A browser that a
  page's rebinding name sends to the container's own loopback therefore
  reads nothing from the API. Empty, the default, accepts any `Host`; when
  set it must include `127.0.0.1`. Refusals count in
  `scrapeyard_host_refusals_total`.
- A host check on a timer (TD-20): `security/health-check.py`, installed as
  `scrapeyard-health` with `scrapeyard-health.timer`, checks readiness, disk
  and inodes, the backup's age, the policy watch, failed units, restarts and
  the tunnel's connector every five minutes, and reports through
  healthchecks.io or ntfy URLs in a root-only file, or a root-owned command.
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
- Added a deployment rehearsal (`scripts/run_deployment_rehearsal.sh`, the
  `Release artifact and deployment rehearsal` workflow). It builds two retained
  releases on a GitHub runner and runs the production Compose file through
  each release's own deploy wrapper: a deployment, a refused corrupted
  release, a failed and a missing egress policy, a rollback and a roll-forward.
  A manually dispatched run uploads the rehearsed release.
- Added a systemd unit (`security/systemd/scrapeyard.service`) that restores
  the host egress policy and the AppArmor profile and starts the promoted
  release after a boot and whenever Docker is started or restarted. A failed
  start is retried with growing pauses and leaves the application stopped.
  `security/manage-release.sh` (installed as `scrapeyard-release`) has a
  `promote` command that deploys a retained release as one
  systemd unit and then makes it the release the boot service starts; it
  refuses a release root does not own and a settings file it cannot read,
  without printing the line. The deployment rehearsal exercises both with
  `--boot-service`.

### Changed
- The image's Python moves from 3.12.11, its base since 0.6.0, to 3.14.8
  (`python:3.14.8-slim-bookworm`, pinned by digest; TD-116). The image sets
  `LANG=C.UTF-8` itself, as the python images stopped doing after 3.12:
  unzip needs it for Camoufox's font names, and the browsers keep the locale
  they ran under. Python 3.12 stays the minimum, for the host's scripts. CI's
  quality gate and the dependency audit run on both; mypy checks against
  3.12, and the distribution build, live Redis and the real-browser tests run
  on 3.14.
- Work whose caller stops waiting (a readiness probe past its timeout, a
  cancelled cleanup, abandoned thread work) runs on under
  `scrapeyard.common.async_tools.shield` instead of `asyncio.shield`. From
  3.14 `asyncio.shield` logs such work's later failure at ERROR with a
  traceback even though Scrapeyard retrieves it; the helper waits with
  `asyncio.wait`, which does not. Ruff bans `asyncio.shield` and
  `asyncio.iscoroutinefunction`.
- arq moves from 0.26.3 to 0.28.0, which no longer calls
  `asyncio.iscoroutinefunction`, deprecated from 3.14.
- The host check, the smoke, rehearsal and qualification scripts close each
  `HTTPError` they catch, and `scripts/qualification_backup.py` its SQLite
  connections. Python warns about either left open, from 3.14 and 3.13.
- Python 3.12 is the minimum supported version, up from 3.10. It is what the
  production image runs and what `python3` is on the Ubuntu 24.04 production
  host. CI and the dependency audit no longer run a 3.10 leg, and the code
  drops its 3.10 handling of `asyncio.TimeoutError`. The lock no longer needs
  a numpy that runs on 3.10, so the image's numpy, which only camoufox uses,
  moves from 2.2.6 to 2.4.2, the version already locked for 3.13 and later.
- A worker that has finished no poll of the queue for 60 seconds counts as
  ended (TD-92): readiness fails with `worker runner task stalled` and the
  process ends itself as for TD-89. arq gives its Redis connections no read
  timeout, so a Redis that stopped answering without closing held the poll
  for ever while readiness's worker check stayed ok and accepted runs stayed
  `queued`. A held-up event loop alone is not taken for a stall: two looks a
  quarter of the bound apart must find the same poll overdue. A shutdown no
  longer waits on such a Redis to remove the worker's health key, which
  expires by itself; before, that removal failed the stop.
- An application whose embedded worker ends outside a shutdown, as it does
  when Redis goes away under it, now ends itself (TD-89): the server's own
  shutdown gives running jobs the grace, a thread ends the process with
  status 70 if that has not finished ten seconds later, and the container's
  restart policy starts it again through a full start. Before, it kept
  answering submissions with `202` and never ran them until an operator
  restarted it. The next start releases the queue deliveries the ended
  process could not mark finished. A deployment whose container has no
  restart policy is left stopped instead of idle.
- The deploy wrapper hands the container every `SCRAPEYARD_SECRET_*` name
  that `SCRAPEYARD_SECRET_REFERENCE_ALLOWLIST` lets a project use, by name
  only (TD-91), so a job's `${SCRAPEYARD_SECRET_...}` reference resolves
  under the shipped Compose file.
- The container's health check is the application's readiness. Compose and
  the image run `python -m scrapeyard.runtime.healthcheck`, which asks
  `/health/ready` from inside the container with a `health-detail`
  credential from the container's own settings; until now both asked the
  public liveness route, which answers while Redis, a database or a
  background task is gone. `docker ps` shows `unhealthy` otherwise, and
  Docker does not restart an unhealthy container. A deployment's start
  waits on the same check: one whose application runs and does not become
  ready (a failing dependency, free disk under its floor) now fails after
  about two minutes and is left stopped, where it used to be left serving.
  Of several credentials with the scope the check takes the one with the
  fewest scopes; a deployment with none is checked for liveness as before.
- A timer checks the host egress policy every minute between deployments and
  puts a missing one back (`scrapeyard-release watch`,
  `security/systemd/scrapeyard-policy-watch.timer`). The application checks
  the policy once, at its start; a flush of the host's rules, or a restore
  of rules saved earlier, removes the policy's jumps, the `INPUT` one that
  keeps the application off the host's own addresses included. The
  installer gained a read-only `check` action for it, and no longer writes
  bytecode into the tree it runs from. A policy that cannot be put back
  stops the application and restarts the boot service, once for a finding;
  what the application's own start check does not cover (the `INPUT` jump,
  an IPv6 policy) can then leave it running without that part, with the
  watch failing every minute. Install the newer `scrapeyard-release` with
  the two units.
- The pinned container runtime is Docker 29.9.0, containerd.io 2.4.1 (runc
  1.5.2) and Compose 5.6.0, up from 28.5.2, 1.7.29 (runc 1.3.3) and 2.38.2.
  A host is upgraded in place, which keeps its classic image store, and
  rebooted.
- The container lanes select Docker's classic image store and fail on any
  other, and the deployment guide tells a host to keep it. A release names
  its images by identifier, and an image has another identifier under
  containerd's image store, which a new installation of Docker 29 chooses:
  a release built on one store is refused on the other. Tried on Docker
  29.9.0 in both directions; a release built on 28.5.2 loads and runs on
  29.9.0 with the classic store, which is what an upgrade in place keeps.
  `release_artifact.py load` now says so when it refuses, and the rehearsal
  records the store in `container-runtime.txt`.
- The seccomp profile (`security/seccomp/chromium.json`) is now the default
  profile of Docker 29.9.0 plus one rule that allows `clone` and `unshare`
  for the browsers' user-namespace sandboxes. It was an older default, by
  way of Playwright, which also allowed `setns`. The container no longer may
  use `io_uring`, `setns` into another namespace, or call `socket` for the
  `AF_ALG` and `AF_VSOCK` families, and it gains the system calls Docker has
  allowed every container since (`faccessat2`, `close_range`, `pidfd_open`,
  `epoll_pwait2`, the `landlock` calls and others).
  `scripts/derive_seccomp_profile.py` writes the profile from the copied
  default, and `security/seccomp/README.md` records the difference.
- The AppArmor profile denies `AF_VSOCK` sockets, as Moby's own profile
  does. The seccomp filter covers the direct `socket` call only, and a
  64-bit process can still reach `socketcall` through the 32-bit entry;
  until now nothing closed that path for `AF_VSOCK`.
- The AppArmor profile denies the `net_admin` and `net_raw` capabilities and
  packet sockets (TD-87). The seccomp profile lets any process in the
  container create user namespaces, which the browsers' sandboxes need, and
  the kernel gives a process every capability inside one it creates,
  whatever the container's bounding set holds. With a network namespace of
  its own it could open packet and raw sockets, create netfilter tables and
  links, and configure traffic control. AppArmor applies a capability denial
  inside a user namespace too, so each of those is now refused there, while
  the namespaces, `chroot` and route reads the sandboxes use still work. The
  smoke lane tries each from a namespace of its own, and under a control
  profile without the denials, where they succeed. An independent review
  tried the routes round the denial (nested and mapped namespaces, `exec`,
  the 32-bit entry, profile changes, `docker exec`) and found none.
- `security/deploy-secure-compose.sh` requires the application to run under
  its AppArmor profile, as the kernel reports it for the container's first
  process, and leaves the application stopped otherwise. Where Docker cannot
  use AppArmor it starts a container unconfined under the profile's name,
  and every denial above is the profile's. A host whose Docker reports no
  AppArmor at all is refused before anything is stopped.
- The container smoke lane opens `AF_ALG` and `AF_VSOCK` sockets through
  the 32-bit `socketcall` entry from inside the application's container and
  requires AppArmor's refusal, and runs the same attempts under a copy of
  the profile without the two denials, where they have to succeed
  (`tests/smoke/verify_confinement.py`). Until now the denials rested on
  Moby's statement that AppArmor's check does not depend on the entry used.
- The container smoke lane and the release rehearsal replace the runner's
  Docker with the versions the deployment guide now names for a host
  (`scripts/install_container_runtime.sh`: Docker 28.5.2, containerd.io
  1.7.29 with runc 1.3.3, Compose 2.38.2) and fail when anything else runs.
  The rehearsal records them in `container-runtime.txt`. Until now both ran
  on the runner image's Docker, under whose runc a release passed that
  started no container on the production host.
- `security/deploy-secure-compose.sh` deploys without `SCRAPEYARD_PROXY_URL`
  when `SCRAPEYARD_UNTRUSTED_SUBMISSIONS` is `false`, for a deployment whose
  only submitter is its operator's own service. Otherwise it still requires
  the proxy. In either mode it now refuses a deployment that sets the egress
  probe's address or ports, which the Compose file fixes, or a probe timeout
  under one second, so the application's startup check of the host egress
  policy cannot be configured away.
- A non-ASCII hostname is now accepted only when IDNA 2003 and strict IDNA
  2008 encode it to the same name; any other is rejected as an unsafe URL, in
  submitted configs, in `browser.third_party_allow_hosts`, and in URLs met
  during a run. The URL guard encodes hostnames with Python's IDNA 2003 codec
  while httpx and browsers use IDNA 2008, so for `straße.test` the guard
  checked and pinned `strasse.test` while the client asked for
  `xn--strae-oqa.test`. Names IDNA 2008 does not allow (`☃.test`, fullwidth
  spellings) are rejected as well: httpx refuses them and a browser may map
  them to another name. The ASCII `xn--` spelling of any rejected name is
  accepted. ASCII hostnames are unaffected, except that one containing `<`,
  `>` or `^` is rejected, because httpx percent-encodes those and would
  request a different name. `idna` is now a declared dependency.
- Within one run, the URL guard now resolves a hostname once per 30 seconds
  instead of on every check. It used to hop to a thread and call the resolver
  for every browser subrequest, and four to six times for each top-level
  page. A hostname the run has just resolved to public addresses only is now
  passed after the lexical checks alone, which still run on every URL, and
  requests that arrive while a name is being resolved wait for that one
  answer. A name that does not resolve, or resolves to a non-public address,
  is never remembered, and such an answer drops the verdict the run held.
  URLs over 2,048 characters or not in ASCII are resolved every time. Direct
  basic fetches still resolve for, and pin, every connection. On a fixture
  run of 5 pages and 255 browser requests to 11 hosts, lookups fell from 274
  to 11 with the same records and traffic counts; wall time fell from 6.2 s
  to 3.5 s with a resolver that takes 25 ms an answer (as on the development
  host) and from 3.7 s to 3.5 s with one that takes 1 ms.
- GitHub Actions workflows now pin the Node 24 releases of their actions by
  commit SHA: `actions/checkout` v7.0.1, `actions/setup-python` v7.0.0,
  `actions/cache` v6.1.0 and `actions/upload-artifact` v7.0.1 (from v4, v5, v4
  and v4, which target the deprecated Node 20 runtime). Every input the
  workflows pass exists in the new releases. Actions is still disabled, so
  the first run after it is re-enabled is what confirms them.
- `POST /jobs/{job_id}/cancel?run_id=…` now accepts the `submit` scope for an
  exact run of an ad-hoc job in the caller's project, so a client that may
  start a run can stop it. Eyebox's `submit` and `read` credential used to get
  a 403, and the runs it abandoned kept running until their budget ran out.
  Cancelling without `run_id`, cancelling a scheduled job (which also disables
  its schedule) and `DELETE /jobs/{job_id}` still require `delete`.
- Updated locked dependencies with known advisories: aiohttp 3.14.4, anyio
  4.15.1, cryptography 50.0.2, multidict 6.9.1, PyJWT 2.15.1 and urllib3
  2.8.0 (with yarl 1.25.1 and typing-extensions 4.16.0). `cryptography` is
  now constrained to `>=50.0.2,<51.0.0`; its 49 and 50 releases change
  nothing in the AES-GCM API that secret storage uses. Both dependency
  audits and the container scan are clean, with no exceptions.
- Each fetched page is now freed as soon as the engine drops it, instead of
  waiting for the cyclic garbage collector, so parsed pages no longer pile up
  during pagination. On a 50-page link pagination run with 265 KiB pages,
  peak RSS growth fell from 53 MB to 31 MB on Python 3.12 (64 MB to 34 MB on
  3.10), and with 660 KiB pages from 128 MB to 55 MB. Records are unchanged.
- Stale-running recovery, which runs every minute under the jobs.db write
  lock, no longer scans every job. With 2 KiB configs it took 7.7 ms at 6,600
  jobs and 27 ms at 25,000; it now takes under 1 ms at both. Migration `025`
  adds a partial index on running jobs without a current run, which stays
  empty unless that invalid state exists. A binary older than this change
  refuses to start on an upgraded database, so rolling back means restoring
  the pre-upgrade backup.
- The container image builds with pip 26.2.1 (26.1.2 has PYSEC-2026-3721)
  and no longer ships pip at runtime. Its Debian package snapshot moves to
  2026-10-08, which picks up the perl security update. CI and the
  dependency-audit workflow pin the same pip, the live Redis job enables
  append-only persistence, and release qualification no longer assumes the
  runner owns group 1000.
- Development: ruff is upgraded to 0.16, import order is now enforced, and
  the Python helpers under `security/` are linted. The README settings
  table now lists every `SCRAPEYARD_*` setting with its default, checked
  against the code by a test.
- The container image no longer installs Chromium headless shells, which
  are never launched, or C build packages, which no locked dependency
  needs; it is 595 MB smaller uncompressed (4,018 MB to 3,423 MB).
  Dependency installs accept wheels only.
- Packages Scrapeyard imports directly (playwright, rebrowser-playwright,
  camoufox, redis, starlette, tldextract) are now declared dependencies,
  at the versions already locked.
- Split the SQLite job store's retention, deletion, terminal webhook
  reconciliation and recovery code into separate modules; behavior is
  unchanged.
- Removed fetch code paths and helpers that only tests reached, and merged
  duplicated decrypt-and-verify and thread-pool code. Behavior is unchanged.
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
- A project-scoped caller asking any job route for another project's job gets
  what an absent job gets (`404`, or `204` from `DELETE /jobs/{id}`, which
  leaves it alone), not a `403` naming the project (TD-115). The application
  no longer serves `/openapi.json`, `/docs` or `/redoc`, which answered any
  valid credential.
- The URL guard reads IPv4 hosts as browsers do (WHATWG URL): `0x7f.0x.0x.1`
  and `0x`, which passed as DNS names, are 127.0.0.1 and 0.0.0.0, and a host
  ending in a number that is no address is refused. The domain guard keys
  every spelling of an address as one, and `/domains/{host}/guard` takes
  bracketed IPv6 hosts and names with `_` (TD-114).
- A Redis that stops answering while a run is in flight no longer ends the
  application's stop in a failure that keeps the instance guard and leaves
  the databases open: the stop drops the Redis connections arq's result
  writes hang on (TD-107).
- Live basic and browser pages are decoded and parsed on the run's threads,
  not the event loop, where a 20 MiB page held every other run and the API
  for a third of a second.
- `security/backup.sh` counts Redis's keys in every database. It counted
  the first alone, so with `SCRAPEYARD_REDIS_DSN` naming another one
  every backup recorded 0 keys and every restore of it failed when Redis
  loaded more. A refused `INFO` fails the backup rather than reading as
  no keys.
- `block_third_party` no longer blocks the requests of a target whose
  host is a Unicode name to its own site (`www.bücher.de` loading from
  `cdn.xn--bcher-kva.de`), and `run_budget.traffic` no longer counts them
  as third-party: sites are now compared in the ASCII form the browser
  reports requests in, whichever form a request was recorded in.
- The host check fails `backup` when the last backup completed more than
  15 minutes in the future. A backup made while the clock was ahead read
  as fresh until the clock passed it, however many runs failed meanwhile.
- `security/backup.sh` refuses to take a witness, with nothing stopped,
  when no configured credential holds the `read` scope. The witness then
  listed no job, and every restore test of such a backup reported the job
  list "identical to the witness" and passed without comparing anything.
- A `SCRAPEYARD_STORAGE_RESULTS_DIR` reached through a symlink (a bind to
  another disk, or `/tmp` on macOS) holds results. The path check resolved
  the root but not the paths stored under it, so every result save failed
  as outside the directory while readiness, which writes the directory
  itself, passed. Cleanup now scans and prunes such a directory too: it
  never pruned an emptied project or job there. Links below the root are
  still refused. The production volume is not a symlink.
- The browser asset cache's 32 MiB bound counts each entry's URL and
  headers as well as its body. Only bodies counted, so a page that had the
  browser fetch cacheable scripts with empty bodies, long URLs and large
  headers kept them all for the run: 2,000 such responses held about
  294 MiB against an accounted size of 0. Each header counts as Python
  holds it, so neither many short headers nor wide characters are
  undercounted severalfold.
- `execution.page_cache: record` no longer writes a resolved deployment
  secret, or a URL's userinfo, to the cache directory in plain text. The
  recorded `final_url` kept both (the query of a target URL holding a
  `SCRAPEYARD_SECRET_*` value, or `user:password@`), and a page that echoed
  a secret was stored with it, outside the encrypted store and with no
  retention, where SECRET_STORAGE.md promised every persisted copy is
  encrypted. A page that would write a resolved secret anywhere (HTML,
  final URL, content type, click snapshots), as sent, percent-encoded
  (twice, or with `+` for a space, included), as HTML entities, in a JSON
  string or in another case, is now not
  recorded (`"stored": false`), and userinfo is dropped from the URL. A
  secret transformed otherwise, such as base64, is not recognized.
- A page that `execution.page_cache: record` cannot write to the cache
  directory (full, read-only, another owner's) is returned as fetched, with
  `"stored": false` in its `debug.page_cache`. The write error failed the
  target as `network_error` after the site had answered, quoted the
  service's cache path, and counted against the host's circuit breaker, so
  three such pages kept every job's requests to that host off for 300 s.
- Startup refuses named credentials that share an `identity` but not their
  `projects`. Idempotency keys and receipts belong to the identity, so a
  credential limited to one project learned from `/scrape/acceptance` that
  another project's key was in use, and that project's name, and a `POST`
  reusing the key answered `409`, blocking the other project's retry.
  A rotation's old and new credentials, which name the same projects, are
  unaffected. DEPLOYMENT.md said keys were scoped by the API key's digest.
- Startup refuses a `SCRAPEYARD_API_CREDENTIALS` that is set but names no
  credential (`{}`, or a blank value). Both passed Compose's check for an
  unset variable and parsed to no credentials, which leaves the API open
  with every scope, as it is for local development, with only a warning in
  the log.
- A `POST /scrape` with an `Idempotency-Key` whose Redis enqueue fails no
  longer deletes the job: the queued run is left to the stale-queue
  reconciler, as a crash at that point leaves it, and the `POST` still
  answers `500`. The receipt is readable from the moment the job commits,
  so while a stalled enqueue was in flight `/scrape/acceptance` answered
  `200` (and a replay `202`) for a run the rollback then deleted; a client
  that had timed out and recovered it, as Eyebox does, polled a `404`. A
  submission without a key is still rolled back.
- A public IP address spelled with non-ASCII digits (`https://８.８.８.８/`)
  is refused like other hostnames IDNA 2003 and IDNA 2008 read differently.
  The guard accepted it while every key derived from the URL refused it: a
  config with such a target failed its whole run, other targets' results
  included, on every fire, and a page's next link spelled so failed its
  target as `http_error` where it now stops as `unsafe_next_url`. Private
  addresses spelled so are still reported as non-public.
- The URL guard refuses a hostname longer than 253 characters, the most a
  DNS name can have, before it encodes it. IDNA encodes a label before it
  checks the label's length, and Python's punycode is quadratic, so a next
  link whose host was 16,000 distinct non-ASCII characters held one of the
  four run threads for 16 s (a 300 KB host, minutes) while it was judged.
  Such a link now stops pagination as `unsafe_next_url` at once.
- A `basic` page that declares `charset=punycode` is read as UTF-8, as
  other charsets no browser uses already were. Python's punycode decoder
  is pure Python and quadratic, and decoding runs on the event loop: a
  1.3 MB page held the API, readiness, the scheduler and every run for
  34 s, past every timeout, and a page at the byte ceiling would have held
  them for days.
- A compressed `basic` response is inflated a bounded piece at a time
  (64 KiB, or about 2 MiB for zstd), and each piece is charged to the run's
  fetched-byte budget before the next is decoded. httpx decoded each network
  read whole, so a few kilobytes of brotli, or of gzip inside gzip, became
  gigabytes in memory before the budget saw any of it: 1.6 KB of brotli
  took the process to 3.2 GB, and 6 KB past the container's 4 GB limit,
  which would end every run in flight. Such a body now stops at the ceiling
  as `budget_exceeded`, as it already did once decoded. Under stacked
  codings what each hands the next is charged too, so a page sent in gzip
  inside gzip counts its inner gzip as well as itself. A zstd body whose
  window is over 8 MB (the limit RFC 9659 sets and browsers keep) and a
  response with more than four content codings now fail as a decoding error
  (`http_error`).
- DEPLOYMENT.md and JOB_LIFECYCLE.md no longer say a request still running
  at the end of a stop's 5 s request drain gets no answer: Uvicorn answers
  it with a plain-text `500`. For `POST /jobs/{id}/cancel` that `500` can
  come after the cancellation was persisted; a retry answers as any cancel
  of a cancelled run does (`204`, or `503`/`504` while Redis or the worker
  has not settled).
- A native cron schedule's day of week now means what it means in cron:
  0 and 7 are Sunday, so `1-5` is Monday to Friday. APScheduler 3 counts
  from Monday and was given the field as written, so `1-5` ran Tuesday to
  Saturday, `0` on Mondays, and `7` was refused. A schedule stored with a
  numeric day of week fires on different days after the upgrade: the days
  it was written for. Names (`mon-fri`, `sat-sun`) are unchanged, but
  APScheduler ignored whatever followed a name: a step after one now
  applies (`mon-fri/2` is Monday, Wednesday and Friday, not every weekday),
  and a field with other trailing text (`fri.`) is refused. A fire that
  restricts both day of month and day of week still needs both to match,
  as the field's description now says. A stored schedule the scheduler
  cannot register, by its cron or by a time zone the image's zone data no
  longer holds, no longer stops the service from starting: it is left
  unscheduled and reported in readiness as `persisted_config_invalid`.
  Eyebox's schedules restrict neither field. A rollback to an earlier
  release reads numeric days the old way again, and that release does not
  start while a stored schedule names day 7; DEPLOYMENT.md says how to
  check before rolling back.
- A cancellation that reaches a worker just after it recorded its own run
  as `failed` (every target failed, or a run limit) no longer deletes the
  run's result. The worker kept the result only for `complete` and
  `partial`, the statuses only it writes, so its own failed run read
  `failed` with a `404` for the results, losing `targets[]`, `errors[]`
  and `budget_error`. A failed run is now the worker's when it carries the
  `completed_at` the worker gave it.
- A schedule paused while one of its fires was loading the config no
  longer queues that fire after the pause has returned `200`. The fire
  checked `schedule_enabled` before loading the config, and the
  reservation that queues it did not check it again.
- A native cron schedule that fires in the repeated hour of a clock change
  (`schedule.timezone` with daylight saving, the default 120 s jitter) no
  longer hangs the whole process. APScheduler added the jitter in wall
  time, which loses the hour's fold, so the second 01:30 plus jitter fell
  an hour back and its catch-up loop never reached the present, holding
  the event loop: the API, readiness and the worker stopped. The jitter is
  now added in UTC. UTC schedules were never affected, and Eyebox uses no
  native schedule.
- The browser smoke lane's check that the egress policy blocks a private
  and a metadata address now requires the policy's refusal (curl exit 7).
  It accepted any failure, and the private fixture is on an internal
  network Docker isolates whether or not the policy is installed, so the
  check passed without the policy.
- `install_container_runtime.sh` gives apt only Docker's key, exported from
  a throwaway keyring into `/etc/apt/keyrings/docker.gpg`, and never the key
  file Docker's repository serves. It compared only the first key's
  fingerprint in that file and then gave apt the whole file, so another key
  beside Docker's (in a second block, or in a block gpg does not list as a
  public key, which apt still reads) would have been trusted to sign the
  Docker packages the container lanes and release builds install.
- The release workflow no longer uploads a release whose Redis and egress
  probe were never scanned. `create` runs the scanner of the revision it
  builds, and before TD-99 that scanner read only the application's image
  and passed over the others, so rebuilding an older release (its artifact
  expires after 30 days) shipped them unscanned under a summary that said
  "scanned". The workflow now checks the packed candidate with the new
  `release_artifact.py verify --scanned`, which requires a vulnerability
  report naming each image; the rollback target it also builds may predate
  TD-99. Releases built before this change have no `--scanned` in their own
  copy of the script; run it from a current checkout.
- `release_artifact.py verify` (and so `create` and `load`) now refuses a
  release whose `images.tar` lacks an image its manifest names. The
  checksums cover whatever was saved, and the rehearsal's daemon already
  holds every image it built or pulled, so an image left out of the save
  passed every gate until a host's `load` refused the release.
- A config error no longer quotes a piece of a resolved deployment secret
  when the config holds a key spelled like a union member's name. Such an
  error's location names the member (`SelectorLong`, say), and a key of
  that name led the check for messages about a secret-bearing value away
  from the secret, so a transform's message quoted the piece of the
  secret it split off. A message is now also withheld when the value it was
  raised for holds a resolved secret, in any letter case.
- The exact run of a missed scheduled fire whose queued copy outlived it
  is read instead of answering 500. When enqueueing a scheduled run and
  then failing it both raised, the run row had no config but the queued
  copy remained, and the read decrypted that copy with the run row's
  associated data, which fails authentication.
- A restart no longer gives a missed scheduled fire its parent's config.
  The fire's run is recorded without one on purpose, and with no queued
  copy either its exact read (`GET /jobs/{id}/runs/{run_id}`) is a 404, but
  the startup backfill meant for runs from before migration 018 filled it
  in, and the same read answered 200 after the restart. The backfill now
  skips runs with a failure code, which only this version writes.
- A deployment secret reference in a target's browser `extra_headers` is
  refused when the config is validated (TD-102). The browser follows a
  server's redirect itself, past the route that keeps these headers to the
  target's origin, so a redirect to another origin received the secret.
  The field's description said redirects were covered. Eyebox's configs
  use no secret references.
- A cancel request cut off while it waited for a running run to abort is
  cancelled, instead of reporting the run quiescent. arq's `Job.abort`
  answers True when it is itself cancelled, so the caller's cancellation
  read as a finished abort; the abort is now awaited shielded. TD-96's
  5 s limit on Uvicorn's wait for requests at a stop makes such a cut-off
  likelier.
- A page nested deeper than Python's recursion limit (about 1,000
  elements) no longer fails its target with `RecursionError` reported as
  `http_error`: a selector's text is gathered with a stack instead of
  recursion, in the same order.
- `SCRAPEYARD_WEBHOOK_MAX_DELIVERY_ATTEMPTS` is at most 100 and
  `SCRAPEYARD_WEBHOOK_MAX_DELIVERY_AGE_SECONDS` at most 30 days. Unbounded,
  attempt 1,025 overflowed the backoff after its POST, leaving the delivery
  due at once, so the receiver got POSTs with no backoff; and an age past
  the date range stopped every webhook while readiness stayed ok. The
  backoff no longer computes a power past the cap.
- A `regex` transform whose replacement names a group its pattern lacks
  (`regex("(\d+)", "\2")`) or holds a bad escape is refused when the
  config is validated. It was accepted and failed only on a page whose
  text matched, as an `http_error` with no status. A transform's timeout
  or a selector value past `SCRAPEYARD_TRANSFORM_MAX_VALUE_BYTES` is now
  `selector_engine_error` too, not `http_error`: the page was fetched.
  Every Eyebox config still validates.
- A `422` for a config that resolved a deployment secret no longer
  returns the secret in another spelling or in pieces. Only the exact
  secret and its URL-quoted forms were redacted from Pydantic's text, which
  quotes the input cut short or escaped; a message can give it lower-cased
  (a URL scheme); and a transform pipeline, split at `:` and `|`, quotes
  one piece of it. The error is now built from Pydantic's messages without
  the input. A message about a value that held a secret is withheld, its
  location kept, and each secret is redacted from the rest as written,
  quoted or escaped, in any letter case.
- `GET /errors?since=` with a date whose offset puts it outside the
  representable range (`0001-01-01T00:00:00+01:00`) answers `400` instead
  of `500`.
- The live Redis CI job and `scripts/run_live_redis_tests.sh` fail when
  their Redis cannot be reached, instead of passing with every test
  skipped (`SCRAPEYARD_LIVE_REDIS_REQUIRED=1`); the runner's Redis, which
  has no password, is published on loopback only.
- A stop that lands during the egress policy's switch no longer leaves two
  policy jumps behind and reports success (TD-101). The installer marked
  each family switched only after inserting the new jump, so a SIGTERM or
  SIGINT between the two rolled back nothing, printed "prior policy
  restored" and exited 0; SIGHUP was not trapped at all. Every later
  install then refused the two jumps, leaving the application down. Each
  flag is now set before its insert, HUP is trapped too, and an
  interrupted install exits with its signal's status (129, 130 or 143).
- The host check no longer starts a Docker the operator stopped (TD-100).
  It asked the Docker CLI first thing every five minutes; with Docker
  stopped, its socket starts it for the first client, and the boot service,
  which Docker wants, then started the application in the middle of
  maintenance. The check now asks Docker only while `docker.service` is
  active or starting, and otherwise reports `deployment` failing with
  Docker's state.
- Redis is now 7.4.11 (TD-99). The 7.4.5 image every release shipped has
  CVE-2025-49844, remote code execution through a Lua script (CVSS 10),
  and CVE-2025-46817, -46818 and -46819, all fixed in 7.4.6, and its
  `gosu` was built with Go 1.18.2. Nothing caught it because only the
  application's image went through the vulnerability gate: `release_artifact.py
  create` and the container-security workflow now scan the Redis and
  egress-probe images too, with the same HIGH/CRITICAL gate. The probe is
  now Python 3.12.15 on Alpine 3.24, which scans clean. One finding in the
  Redis image, an OpenSSL DTLS issue (CVE-2026-84782) published after it
  was built, is accepted until 2026-11-30 in
  `security/trivy-dependency-exceptions.yaml`: Redis makes no DTLS
  handshake. The live Redis tests and the backup's dump load pass on 7.4.11.
- The image installs the package with pip, which fetched whatever
  `poetry-core` PyPI served at build time, unpinned, as the build backend
  (TD-98). `pyproject.toml` now pins `poetry-core==2.3.2`, the backend
  Poetry 2.3.4 itself uses, so the image and CI build the package with the
  same backend; a test holds the pin to the toolchain check's.
- Two fetch failures were reported under the wrong `error_type` (TD-97).
  A browser error quotes the URL it was loading, and the proxy markers
  were matched against it, so a page whose URL held `407`, `proxy` or
  `tunnel` failing for any reason was `proxy_rejected`; the markers now
  read the error with its URLs removed, and the bare `407` is gone (a
  proxy's refusal names the proxy). And a challenge, consent or login page
  served with a success status other than 200, which the fetch already
  refused for that signal, was `http_error`; it now keeps its
  `challenge_page`, `consent_gate`, `login_gate` or `blocked_response`.
- A stop no longer lets the worker start runs while Uvicorn waits for the
  requests in flight, and that wait no longer holds the shutdown open
  (TD-96). Uvicorn ran the application's shutdown, which is what stopped the
  worker, only once every request had ended, without limit: a slow request
  at a `docker stop`, a backup stop or a promotion let new runs start after
  the signal and pushed the end of the shutdown towards Compose's SIGKILL at
  40 s, leaving the runs it cut off `running` until reconciliation. Now the
  worker polls for no run from the signal on (a start already under way,
  a matter of milliseconds, still finishes), Uvicorn waits at most 5 s
  (`--timeout-graceful-shutdown 5` in the image and Compose commands), and
  the shutdown grace counts from the signal. With a request held open for
  25 s, the process now ended 9.6 s after SIGTERM instead of 34.5 s, and no
  run started after it instead of two.
- Cancelling a run that was still queued answered `504` after the abort
  grace whenever every worker slot was busy, and the delivery was only
  removed once a slot came free (TD-95). arq removes an aborted delivery
  only when its worker picks it up. A delivery that has not started is now
  taken out of its queue, with its payload, by one Redis script that
  refuses if the worker has already marked it in progress, and the cancel
  answers `204` at once. A run that has started is aborted as before.
- A `regex` transform can no longer exhaust the application's memory when
  its config is validated (TD-94). The regex module's compiler copies a
  counted repeat's body once for each of its minimum count: a 29-byte
  pattern, `((((a{100}){100}){100}){100})`, took 70 s and about 28 GB to
  compile inside the submission, and the module's cache kept every pattern
  compiled. The module's own parser now gives the counts before the
  compiler runs: a pattern may build 10,000 nodes and one config's
  patterns 100,000 together, a character range or set counting the
  strings full case folding (`(?fi)`, or `(?i)` under `V1`) adds to it.
  Patterns are no longer cached, and one nested too deeply for the parser
  is an invalid config instead of an unhandled `RecursionError`. Every
  Eyebox config still validates.
- A target under `.localhost` (`http://internal.localhost:8080/`) is
  refused like `localhost` itself (TD-93). Browsers answer every such name
  with loopback without asking DNS, so a browser fetch reached a page
  served on the container's loopback. `localhost6` and
  `localhost6.localdomain6` are refused too.
- A backup that found more than 1,000 jobs accepted after its witness wrote
  an archive every restore refused: it now names the first 1,000 and counts
  them all, and only the restore test refuses such an archive, saying why.
  An archive from before the late-job list is refused by the restore test
  with that reason instead of as one the script does not write (TD-21).
- `scripts/qualification_backup.py create` no longer leaves `-wal` and
  `-shm` files beside the databases it reads: a quiesced database is read
  as immutable, one with pages in its log through the log (TD-21).
- The application's container starts under runc 1.3.3. That version opens
  `/proc/thread-self/fd` with `openat2` after loading the seccomp filter, and
  the profile, which predates that system call, refused it: no container or
  `docker exec` started on a host whose packages carry it. (runc 1.5.2 starts
  under the old profile.) The profile first allowed `openat2` and named the
  system calls numbered just below it, because with `openat2` listed alone
  `clone3` answered `EPERM` and glibc then cannot start a thread. It has
  since been replaced by one derived from Docker's current default (under
  Changed), which allows `openat2` and answers `ENOSYS` to `clone3`.
- The egress probe's health check costs a fifth of a second of its quarter
  CPU instead of five. Its image carries no bytecode and its filesystem is
  read-only, so every check compiled `asyncio`, `argparse`, `secrets` and
  `socket.py` again; on a host with slower cores that passed the check's
  three-second deadline and no deployment started. The check now imports
  only the C socket module beyond what the interpreter has already loaded.
- A deployment whose application container cannot be stopped removes it. A
  container that died before it was up was restarted without end, and
  `docker stop` failed with "did not receive an exit event", so the deploy
  wrapper reported the application stopped while Docker went on restarting
  it. The wrapper now removes a container of the service that is not `exited`
  after the stop, keeps its last output in a file only root can read, and
  says so when it cannot remove it. A signal that arrives while a failed
  start is being cleared no longer cuts the clearing short.
- Replaying a recorded page whose next link names a refused host stops
  pagination with `unsafe_next_url`, as the live run does. Replay skipped the
  URL check and then failed the whole target while building the link's
  pagination key.
- `url_host_label` reports a URL it cannot parse as an unsafe URL instead of a
  bare `ValueError`.
- The browser smoke lane packs its failure diagnostics into one archive
  before uploading them. Result artifacts sit under directories named
  `host:port`, and the upload refused any path containing a colon, so a lane
  that failed after its first scrape uploaded nothing.
- A hostname containing a lone surrogate is refused as an unsafe URL. The
  check for legacy IPv4 spellings let `UnicodeEncodeError` escape from the URL
  guard, so a caller that handles the guard's `UnsafeURLError` saw an error
  type it did not expect.
- A browser fetch no longer waits without limit for a crashed page to be
  released. After a renderer crash Chromium 145 left the context's
  `set_offline` call unanswered while the driver stayed connected, so the
  fetch's cleanup, and with it the target, hung until the run's deadline.
  Releasing a page now has 10 seconds, after which the whole browser session
  is stopped and the fetch's own failure is reported.
- Chromium now runs inside its own sandbox on every dynamic fetch. Scrapling
  asks Playwright for the sandbox only when `stealth` is on, and Playwright
  starts Chromium with `--no-sandbox` otherwise, so fetches without `stealth`
  had only the container's confinement around the renderer.
- The host egress policy installer no longer refuses to run after the
  deployment network has been removed and created again. The jump left for
  the old bridge names an interface that no longer exists; `install` and
  `remove` now delete it.
- `install-docker-egress-policy.sh remove` no longer empties a policy chain
  that another interface still jumps to, which left that jump filtering
  nothing. It leaves the chain and exits with an error.
- `deploy-secure-compose.sh` now leaves the application stopped when its start
  fails or the deployment is interrupted. The container restarts by itself, so
  a failed start used to leave it retrying outside any deployment.
- Deployments and policy installations take a lock under `/run/scrapeyard`.
  Two that overlapped could each undo the other's policy switch and leave no
  jump installed.
- A retained release no longer keeps files that group or others can write,
  and the release archive records root as the owner of every file.
- The host egress policy now also covers the host's own addresses. A
  connection from the application to the bridge gateway or to the host's
  network address is input to the host, not forwarded traffic, and passed no
  rule: with the policy installed, the application's container connected to a
  listener on the gateway. The installer adds an `INPUT` chain on the
  deployment bridge that refuses everything from the application's address
  except replies to connections the host opened.
- `release_artifact.py create` no longer fails at random with "invalid
  reference format". It named its image after a random temporary directory,
  and about one such name in twenty put an underscore beside a hyphen, which
  Docker refuses.
- `scrapeyard-release` now takes its lock before it reads the `current` link.
  A start of the boot service that had waited for a promotion deployed the
  release the link named before the wait, replacing the one just promoted.
- `scrapeyard-release` refuses a release, or a `current` link, under a
  directory that another account can write to, and accepts no `COMPOSE_`
  setting but the project name. Its `stop` no longer reports success when
  Docker did not answer, and no longer depends on the rest of the settings
  file. A promotion that was stopped is judged once its unit has finished
  stopping, and one whose link cannot be moved stops what it deployed.
- The host egress policy now requires the kernel's bridge netfilter hook
  before it installs. The policy's `DOCKER-USER` rules see traffic between
  containers on the deployment bridge only while
  `net.bridge.bridge-nf-call-iptables` is on, and Docker Engine has not
  turned it on by default since 27.4. On such a host the chains installed
  cleanly and filtered nothing on the bridge; the startup attestation caught
  it and refused to start ("controlled non-public challenge was reachable").
  `install-docker-egress-policy.sh` now loads `br_netfilter` and sets the
  hook before it changes any chain, and stops if it cannot.
- The browser smoke lane finds the image it built by name. It looked for
  Compose's labels, which Compose 2.38 no longer puts on a built image, and
  stopped with "Compose did not produce a Scrapeyard image".
- The release-artifact unit test no longer depends on the host's free disk.
  `release_artifact.create` refuses to run with under 15 GiB free where the
  release is written, and the test let that check read the real temp
  filesystem: with less free space the passing case failed, and the three
  failure cases passed without reaching the failure they name. The test now
  supplies the free space, covers the refusal itself, and checks that each
  other case fails for its own reason.
- A refused URL is no longer logged as an unhandled error when the request
  that asked about it has gone. Run-thread work whose caller was cancelled or
  ran out of budget kept running with nobody awaiting it; when it then
  failed, as the URL guard does to refuse a URL, asyncio logged `Future
  exception was never retrieved` at ERROR with the traceback. The same
  happened to a readiness probe that failed after its response timed out.
  Both now retrieve the late failure.
- The fetch-session smoke check no longer fails when Chromium asks for
  `/favicon.ico` by itself. In its browser-disconnect step the test closes
  the browser under a live page; a favicon request issued in those
  milliseconds reaches the fixture without ever being handed to the route
  guard, so the fixture saw two requests where the run counted one (about
  half the local runs on the stealth path). The check now compares the page
  requests only. `docs/DEPLOYMENT.md` records the two browser races the
  application accepts and leaves to the egress policy.
- Behind a proxy, a browser subrequest whose host doesn't resolve is now
  dropped on its own, and recorded in the fetch's `blocked_requests` debug
  data as `unresolved_request`, instead of failing the whole page and
  relaunching the browser on every attempt. The request is still never sent,
  and an unresolvable main document still fails the attempt and is retried.
- The connected-address policy now also denies IPv4-compatible (`::/96`),
  IPv4-translated (`::ffff:0:0:0/96`) and site-local (`fec0::/10`) IPv6
  destinations, which could spell a local IPv4 address such as `::7f00:1`.
  The host egress policy is rendered from the same table; install it again
  to apply the new rules there.
- Shutdown no longer keeps the databases open, leaving the container to be
  killed at its `stop_grace_period`, because a thread was still finishing a
  DNS check, a page parse or a result render for a cancelled job. That work
  uses neither the databases nor the webhook dispatcher, so both now close
  first and the thread gets the rest of the grace. If it outlasts the grace,
  shutdown still reports failure and keeps the instance guard, with the
  databases already closed. Unresolved worker or background tasks keep them
  open as before.
- A basic-fetch page whose declared charset can't give parseable text is now
  decoded as UTF-8 instead of failing its target as `http_error`. That covers
  `utf-7`, `unicode_escape` and `raw_unicode_escape` bodies that decode to
  surrogate code points, and codecs such as `idna` that reject replacement.
- A cancellation that lands while a crash is being recorded, such as a
  shutdown cancel during its database writes, no longer leaves the run
  `running` until the heartbeat timeout. It gets the same recovery as any
  other cancellation, so the run ends `failed`.
- Deployment secret redaction orders equal-length secret variants by value,
  so overlapping ones redact the same way in every process. The order used
  to follow the per-process string hash seed.
- A result download whose client stops reading is abandoned after 30 s
  without progress, and its connection closed, which frees the rendered body.
  It used to stay in memory until TCP gave up, which can take minutes, and
  uncounted against `SCRAPEYARD_API_RESULT_THREAD_MAX_WORKERS` after 20 s.
  Eyebox gives up on a whole download after 30 s, so this never cuts one off
  that it is still waiting for.
- On Python 3.10, a browser navigation timeout is now reported as
  `navigation_timeout`, and a builtin `TimeoutError` from a basic fetch as
  `timeout`, as on 3.11 and later. They used to come out as `browser_error`
  or `network_error`.
- A submitted config holding a surrogate code point (a YAML escape from
  `\uD800` to `\uDFFF`) in any key or value is now rejected with a 422 that
  names its location. In a selector field name it used to let the run complete
  with records that made every `GET /results` for that run fail with a 500;
  in URLs, selectors and transform arguments it failed the target as
  `http_error`. A stored config holding one fails its run before fetching.
- Redacting URLs in error text, YAML errors and `GET /jobs/{id}` configs no
  longer takes quadratic time on a long run of letters, digits, `+`, `.` or
  `-` with no `://`. A 256 KiB config holding one such token cost about two
  minutes of CPU per redaction; it now takes milliseconds. The URLs found are
  unchanged.
- A cancellation that lands while a run-budget stop is being recorded, such as
  a shutdown cancel during its database writes, no longer leaves the run
  `running` until the heartbeat timeout. It gets the same ownership check and
  shielded crash finalization as any other cancellation, so the run ends
  `failed`.
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
