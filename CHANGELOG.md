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
