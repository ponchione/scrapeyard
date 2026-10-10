# Technical debt and PR roadmap

This is Scrapeyard's working backlog. Unless an entry says otherwise, **each
open entry is one pull request**, and its lettered sub-items are commits within
it. Read [How to use this list](#how-to-use-this-list) and the
[Eyebox contract](#eyebox-contract) before starting any entry.

**Audit history:**
- **2026-10-09** (`67b6b4b` to `409f08c`): second roadmap pass. TD-51 to TD-59
  and TD-61 were fixed and removed, as were TD-63 and TD-64, found while fixing
  them. The coordinated TD-47 (submit-scope cancel) and TD-46 (unresolved
  subrequests and the IPv6 deny list) also landed. TD-60 was rechecked against
  real browsers, corrected to a frame-detach race rather than a favicon bypass,
  and left for the security review.
- **2026-10-09** (`a36c0a7` onwards): roadmap pass. TD-30 to TD-44 were fixed
  and removed, and TD-26's code-side fixes landed. Findings made while fixing
  them are TD-51 to TD-62.
- **2026-10-09** (`b0b285e`): wide sweep of all of `src/`, storage and SQL,
  build, CI, tests and docs, with an Eyebox integration map (Eyebox at
  `a466ee2`). Added TD-25 to TD-49 and C-1 to C-9, and refreshed TD-17, TD-23
  and TD-24.
- **2026-09-25** (`d0591c7` to `5de7dc9`): traffic and memory pass. Added TD-23
  and TD-24.
- **2026-09-07** (`5be6e47`): first codebase audit plus the
  [deployment assessment](docs/deployment-assessment-2026-09-07.md). Added TD-1
  to TD-22. TD-1 to TD-16 have since been resolved and removed, and that
  recheck did not cover a browser security review.

**Priorities:**
- **P1:** security or service availability.
- **P2:** functional or operational correctness.
- **P3:** efficiency or maintainability. Where the benefit is still a
  hypothesis, measure first.

## How to use this list

1. **Work the [roadmap](#roadmap) from the top.** Coordinated entries need a
   decision, a security review, or an Eyebox change before code. Deferred
   entries need a trigger before anyone works on them.
2. **Use a worktree, never the main checkout.** Eyebox's local stack (container
   `eyebox-ingestion-scrapeyard`) bind-mounts this checkout read-only at
   `/opt/scrapeyard` and imports from `src/`. Edits or branch switches in the
   main checkout change what Eyebox runs on its next restart. Start with
   `git worktree add .worktrees/td-NN -b td-NN-<slug>`; `.worktrees/` is
   git-ignored.
   - A virtualenv built from the main checkout has an editable install that
     points at the main checkout's `src/`. Run the worktree's checks with
     `PYTHONPATH="$PWD/src"`, or the tests silently import the main checkout's
     code. Verify with `python -c 'import scrapeyard; print(scrapeyard.__file__)'`.
   - Merge test-only and doc-only branches whenever they are ready. Merging a
     runtime change into the main checkout is effectively deploying it to
     Eyebox's local stack on its next restart, so time those merges with
     whoever is running Eyebox.
3. **Re-verify before fixing.** Locations name the symbol; any line numbers are
   from `b0b285e` and will drift. Numbers marked *measured* came from throwaway
   2026-10-09 benchmarks, so re-measure before and after in the PR instead of
   quoting them.
4. **Read the Contract field.** "None" means internal only. Anything else means
   checking the [Eyebox contract](#eyebox-contract) and running the Eyebox
   checks in step 5 before merging.
5. **Run the checks locally.** CI is disabled until TD-26 lands.

   ```sh
   poetry run ruff check src tests scripts security
   poetry run mypy src
   poetry run pytest -W error
   ```

   For entries that touch the contract, also run these. The second command
   checks every Eyebox config against this checkout's schema; Eyebox's own
   tests never load Scrapeyard's schema.

   ```sh
   # Eyebox client tests
   (cd ../eyebox/ingest && ./.venv/bin/pytest tests/test_scrapeyard_transport.py \
     tests/test_scrapeyard_identity.py tests/test_scrapeyard_source.py \
     tests/test_scheduled_scrapeyard.py tests/test_scrapejob_diagnostics.py \
     tests/test_price_check.py tests/test_webhook.py tests/test_integration.py)

   # Validate every Eyebox config (56 passed on 2026-10-09; one needs a page-cache dir)
   SCRAPEYARD_PAGE_CACHE_DIR="$(mktemp -d)" poetry run python -c '
   import pathlib; from scrapeyard.config.loader import load_config
   for p in sorted(pathlib.Path("../eyebox/configs").glob("*.yaml")): load_config(p.read_text())
   print("ok")'
   ```

6. **Close out.** Delete the entry's section and its rows in the roadmap and
   index, and add a line under `Unreleased` in `CHANGELOG.md`. For partial
   progress, delete the finished sub-items and say what remains. IDs are never
   reused or renumbered.
7. **New findings** take the next free ID (**TD-65**) and use this template:

   ```markdown
   ### TD-NN — <the problem, stated as a fact>

   **Priority:** Pn · **Area:** … · **Risk:** low|medium|high · **Contract:** none | <what Eyebox sees> · **Depends on:** —

   **Problem:** what is wrong, the evidence, and why it matters.
   **Code:** `path` (`symbol`), …
   **Fix:** …
   **Done when:** the test or measurement that proves it.
   ```

## Baseline

Measured on 2026-10-09 at `b0b285e`.

| Check | Result |
| --- | --- |
| `ruff check src tests scripts` | Passed |
| `mypy src` (strict) | Passed, 101 source files |
| `pytest -W error` | On a WSL host: 150 failed, 2,206 passed (TD-25). With `SCRAPEYARD_WORKERS_MEMORY_LIMIT_MB=0`: 2,355 passed, 17 skipped (live Redis), and 1 expected failure (the test asserting the default). 90% branch coverage, 76 s. |
| `ruff format --check` | 160 files would change; on 2026-10-09 the formatter was deliberately not adopted |
| GitHub Actions | Disabled for the repository. The last runs on `main` (2026-09-07) failed in CI, Dependency audit, and Container security (TD-26). |
| `pip-audit` on pip 26.1.2 | PYSEC-2026-3721, fixed in 26.2 (TD-26) |
| Eyebox configs vs. schema | 56 of 56 validate |
| `pytest -W error` after TD-25 | 2,356 passed, 17 skipped, with no env override and no network lookups; 41–54 s |
| After the 2026-10-09 roadmap pass | ruff 0.16.9 over `src tests scripts security` and mypy (105 files) pass; `pytest -W error` 2,492 passed, 17 skipped, 90.15% coverage on 3.12, and the same counts on 3.10; the 17 live Redis tests pass against a service-style Redis; `pip-audit` finds pip 26.2.1 clean but 32 production advisories in locked packages |
| After the second 2026-10-09 pass (`409f08c`) | ruff, mypy (105 files) and `pytest -W error` pass on 3.12 and 3.10: 2,551 passed, 17 skipped, 90.31% coverage; `scripts/audit_dependencies.py all` and the container scan (image built locally) are clean; Eyebox's client tests (297 passed) and 54 configs pass |

Not run: live Redis tests, container builds, browser smoke, release
qualification.

## Eyebox contract

Eyebox (`../eyebox`) is the only consumer. Eyebox owns scheduling, ingest
recovery, catalog writes and business data; Scrapeyard owns scrape execution and
artifacts. Production pulls. Scheduled runs strip `webhook`, submit YAML to
`POST /scrape` with an idempotency key, poll the exact run, and download the
results. Keep this ownership and the separate single-host deployment. Native
cron, webhooks, horizontal scaling and metadata consolidation have no consumer
requirement.

Mapped on 2026-10-09 against Eyebox `a466ee2`. The client code is
`ingest/scrapeyard_run.py`, `scrapeyard_identity.py`, `scrapeyard_transport.py`,
`sources/scrapeyard*.py`, `scrapejob_diagnostics.py`, `pdp_alias_backfill.py`,
`pipeline/price_check.py` and `pipeline/offer_accuracy.py`. See also the
[Eyebox topology](../eyebox/docs/deployment-topology.md).

**Frozen: changing any of these breaks Eyebox.**

**Requests**
- Eyebox authenticates with `X-API-Key`.
- It sends an `Idempotency-Key` of 74 bytes (the limit is 128) on *every*
  request.
- Bodies are raw YAML with `Content-Type: application/x-yaml`.
- Eyebox's request cap is 256 KiB, the same as `SCRAPEYARD_MAX_REQUEST_BYTES`.
- Responses must be uncompressed: Eyebox rejects any `content-encoding`.

**Endpoints**
- `POST /scrape` returns 202 `{job_id, run_id, status, poll_url}`. Eyebox treats
  400, 401, 403, 413 and 422 as permanent; anything else sends it to
  `GET /scrape/acceptance`.
- `GET /scrape/acceptance` returns `project`, `config_hash`, `job_id` and
  `run_id`. A 404 means the submission was not accepted.
- `GET /jobs/{id}/runs/{run_id}` returns `job_id`, `project`, `name`, `run_id`,
  `config_hash` and `status`, and must work while the run is queued. Status
  must be one of queued, running, complete, partial, failed or cancelled; any
  other value (`deleting`, for example) is an error to Eyebox.
- `GET /results/{id}?run_id=…&latest=false` returns this envelope:
  - Top level: `job_id`, `run_id`, `status`, `project`, `name`, `config_hash`,
    `completed_at` (with a `Z` suffix), `errors[]`, `budget_error`,
    `page_cache` (Eyebox rejects `replay`) and `results`.
  - `targets[]`: `url`, `status`, `count`, `observed_count`, `pages_scraped`,
    `errors[]`, `error_type`, `error_detail` and
    `pagination.{stop_reason,exhausted}`.
  - `targets[].debug`: `final_url`, `page_title`, `main_document_status`,
    `item_selector_count`, `classification`, `html_excerpt` and
    `selector_counts`.
- `POST /jobs/{id}/cancel?run_id=` must return 204 for a `submit` credential
  cancelling an exact run of an ad-hoc job in its project.
- `GET /jobs/{id}`: Eyebox reads the top-level `status` (price check, accuracy).
- Public `GET /health` must return 2xx.
- `DELETE /domains/{host}/guard` is called by the alias backfill.

**Identity**
- `config_hash` is the SHA-256 of the exact submitted body bytes. Never
  normalize or re-dump the body before hashing.
- `name` is the config `name`, not the suffixed ad-hoc job name.
- The same key with the same bytes replays the same `job_id` and `run_id` for
  the 24 h idempotency retention.

**Result rows**
- With `group_by: merge`, rows are a flat list in config target order, each with
  `_source` set to the target host. Eyebox slices the list by each target's
  `count`.
- Injected row fields: `pricing_visibility`, `display_price_text`,
  `stock_status` and `stock_signal`.
- Selector cardinality: no match gives null, one match a string, more than one
  a list.

**Strings Eyebox matches**
- The run statuses listed above.
- `error_type` values: `blocked_response`, `challenge_page`, `consent_gate`,
  `login_gate`, `content_empty`, `rendered_empty`, `selector_engine_error`,
  `selector_miss`, `browser_error`, `budget_exceeded`, `http_error`,
  `http_not_found`, `navigation_timeout`, `network_error`, `proxy_rejected` and
  `timeout`.
- Messages containing `expected at least N results, got M` or `Not attempted`.

**Config schema**
- Every model is `extra="forbid"`, so every key Eyebox sends is frozen. That
  includes `../eyebox/configs/*.yaml` and the price-check job built in
  `pipeline/price_check.py`.
- It also includes keys Scrapeyard ignores: `schedule` on `/scrape`, and
  `adaptive_domain` with `adaptive: false`.
- Tightening validation is a breaking change. For example, 11 configs list
  `403` in `retry.retryable_status`.
- The price check copies these browser keys from retailer targets, so they must
  stay valid: `timeout_ms`, `disable_resources`, `stealth`, `hide_canvas`,
  `real_chrome`, `humanize`, `os_randomize`, `geoip`, `disable_ads`,
  `useragent`, `extra_headers`, `click_selector`, `click_timeout_ms` and
  `click_wait_ms`.

**Size limits:** Eyebox caps downloads at 10 MiB and 5,000 rows, so any new
envelope field uses up that budget.

**Deployment**
- `SCRAPEYARD_*` env names, port 8420, the `src/` layout, `sql/` at the repo
  root, and `scrapeyard.main:app`.
- Compose service names `scrapeyard`, `redis` and `egress-probe`, and the
  network `backend`.
- The `.env.example` placeholders that Eyebox's hard-test scripts rewrite:
  encryption key `replace-with-…` and proxy `https://scrape-proxy.example:8443`.
- Auth scope names, which are mirrored in `../eyebox/scripts/hard_test_env.py`
  `KNOWN_SCOPES`.

**Free: Eyebox does not depend on these.**
- `/metrics` names and labels (Eyebox doesn't scrape Scrapeyard).
- The shapes of `/health/ready` and `/health/live`.
- `/errors` (used only by smoke tests).
- `GET /jobs`, `PUT /jobs/{id}`, pause, resume, trigger and delete.
- `run_budget` and `recorded_at`; `started_at`, `completed_at` and
  `failure_code` on the exact run.
- Webhook payload extras and signing (production strips webhooks).
- The legacy-v0 envelope (tolerated but never requested).
- Transforms and browser options Eyebox doesn't use (TD-49).

## Roadmap

Ready to implement, in order. Each row is one PR.

| # | Entry | P | Scope | Risk | Contract | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | TD-62 | P3 | CI | low | none | After TD-26b |

**Before the first deploy:** TD-26 (re-enable CI and confirm every workflow
passes). CI is GitHub Actions, not deployment, so local checks cover it until
then. A release (TD-22) needs the container scan and qualification workflows
passing, which needs it.

**Coordinated:**
- TD-45: needs Eyebox to move with it.
- TD-23 and TD-60: one security review covering both.
- TD-24: needs real `reuse_browser` runs.
- TD-17 to TD-22: deployment qualification; not single PRs.

**Deferred:** TD-48 and TD-49.

**Consumer-side:** C-1 to C-9 are fixed in Eyebox.

## Index

| ID | P | Status | Finding |
| --- | --- | --- | --- |
| TD-17 | P2 | operational | Eyebox and Scrapeyard limits lack a jointly qualified operating envelope |
| TD-18 | P1 | operational | Production forward-proxy selection and egress qualification are incomplete |
| TD-19 | P1 | operational | Restricted Hetzner HTTPS ingress and Railway credential handoff are incomplete |
| TD-20 | P1 | operational | Secure startup after host/Docker restart and runtime monitoring are unqualified |
| TD-21 | P1 | operational | Unattended coordinated off-host backup and restoration are incomplete |
| TD-22 | P2 | operational | Deployment and rollback lack retained, qualified release artifacts |
| TD-23 | P3 | coordinated | The browser URL guard pays a thread hop and a DNS lookup on every subrequest |
| TD-24 | P3 | coordinated | Browser reuse and its script cache are tuned on fixtures, not real traffic |
| TD-26 | P1 | before first deploy | CI is disabled, and its workflows haven't run since their fixes |
| TD-45 | P1 | coordinated | Bundled browsers are months behind upstream |
| TD-48 | P3 | deferred | Stored configs are duplicated and uncompressed |
| TD-49 | P3 | deferred | Config and engine features Eyebox doesn't use |
| TD-60 | P2 | coordinated | A browser subrequest can complete during the guard's async check if its frame is detached |
| TD-62 | P3 | ready | Pinned GitHub actions run on Node 20 |

## Ready to implement

### TD-26 — CI is disabled, and its workflows haven't run since their fixes

**Priority:** P1 · **Area:** CI, build · **Risk:** low · **Contract:** none · **Depends on:** an operator re-enabling Actions

**Problem:** Actions is disabled for the repository
(`gh api repos/{owner}/{repo}/actions/permissions` returns `enabled: false`),
so nothing gates merges. The last runs on `main` (2026-09-07) failed in CI,
Dependency audit, Container security and Release qualification. The code-side
causes were fixed on 2026-10-09:
- pip moved to 26.2.1 everywhere (26.1.2 had PYSEC-2026-3721), and the runtime
  image no longer ships pip.
- The live-redis job enables append-only persistence with `CONFIG SET`, and its
  admission test re-enables memory admission through a fixture. All 17 live
  tests passed locally against a service-style Redis.
- The Debian snapshot moved to `20261008T000000Z`; a local image scan found no
  Debian findings.
- Release qualification's backup runs as the backup directory's group, not
  1000.
- mypy runs on one matrix leg, and browser smoke also triggers on model and SQL
  changes.
- The locked Python packages with advisories were updated. Both `pip-audit`
  lanes and a local run of `scripts/run_container_security_scan.sh` are clean,
  with no audit exceptions.

**Remaining:**
- **b. Re-enable Actions,** then confirm on `main` that CI (both quality legs,
  build, live-redis), Dependency audit (both legs), Container security,
  Container and browser smoke, and Release qualification (quick and full)
  pass. Browser smoke and release qualification install host security policy,
  so they weren't run locally.
- **c. Optional:** share or cache image layers between jobs. A PR that only
  touches `src/` still does two cold image builds of about 2.5 min each; this
  can't be verified without Actions.

**Done when:** an operator has re-enabled Actions, every workflow passes on
`main`, and `pip-audit` is clean.

### TD-62 — Pinned GitHub actions run on Node 20

**Priority:** P3 · **Area:** CI · **Risk:** low · **Contract:** none · **Depends on:** TD-26b

**Problem:** the workflows pin `actions/checkout` v4, `actions/setup-python` v5,
`actions/cache` v4 and `actions/upload-artifact` v4, which target Node 20.
GitHub runs them on Node 24 and only warns for now.
**Code:** `.github/workflows/*.yml`.
**Fix:** move to their Node 24 releases, pinned by SHA as today.
**Done when:** the workflows run without the Node 20 deprecation warning.

## Coordinated

These need a decision, a security review, an Eyebox change, or real-traffic
data before any code.

### TD-45 — Bundled browsers are months behind upstream

**Priority:** P1 · **Area:** dependencies, browsers · **Risk:** medium · **Contract:** Eyebox's hard-test venv (`../eyebox/.hard-test/scrapeyard-venv`) and the host's `~/.cache/ms-playwright` must move in lockstep · **Depends on:** coordination with Eyebox

**Problem:**
- rebrowser-playwright 1.52 ships Chromium 136.0.7103, which the dynamic and
  stealthy fetchers use. Playwright 1.58 ships Chromium 145.
- Scrapeyard loads hostile pages with the older build.
- The Scrapling `<0.3` pin limits the options: `browser_session.py` calls
  name-mangled Scrapling internals, which is fine only while the version stays
  below 0.3 (0.2.99 is the last 0.2 release).

**Fix:** plan a bump, or route stealth through upstream Playwright.

**Done when:** browser smoke passes, and a qualification run on the Eyebox
retailer set shows the same or better yield and timing.

### TD-23 — The browser URL guard pays a thread hop and a DNS lookup on every subrequest

**Priority/ownership:** P3; browser fetch efficiency. Measured locally; the
guard itself must not change without a security review.

**Problem:**
- `_guarded_async_intercept_route` runs `assert_public_url` through
  `run_thread_work` for every browser subrequest, and each call resolves the
  host again.
- Measured over 6,500 subrequests on 40 hosts:
  - The lexical checks alone take 12 µs per request.
  - The thread hop brings that to 126 µs wall and 347 µs CPU, about 0.8 s wall
    and 2.3 s CPU per run.
  - An uncached resolver answer costs up to 50 ms, which dominates.
- With four run threads, slow lookups also delay page loads. A fixture page
  with 250 subrequests to unresolvable `.test` names spends most of its 5.5 s
  in guard lookups.

**Start here:** [browser_debug.py](src/scrapeyard/engine/browser_debug.py)
(`_guarded_async_intercept_route`),
[url_guard.py](src/scrapeyard/engine/url_guard.py) (`assert_public_url`),
[run_threads.py](src/scrapeyard/common/run_threads.py).

**Work:**
- Evaluate a per-run verdict cache keyed by canonical hostname, with a short
  TTL (for example 30 to 60 seconds).
  - Run the lexical checks inline on every request.
  - Hop to a thread and resolve only when the host has no fresh verdict.
  - Keep rejections uncached, or cache them only as rejections.
  - Keep the `URLResolutionError` behavior when resolution is required (proxy
    or remote CDP).
- Write down the rebinding argument: in both designs the browser resolves on
  its own after the guard.
- Measure on real traffic first. If resolver answers are already cached
  upstream and fast, the thread hop alone may not be worth a guard change.

**Extension (2026-10-09):** each top-level page is also DNS-checked 4–6 times:
- `_fetch_page`, before the fetch and again after it. For basic fetches the
  second check repeats one the redirect loop already did.
- The route guard, for the document request.
- `capture_browser_state`.
- `_pagination_url_is_safe`.
- `_assert_connection_endpoint`, once per attempt for the proxy endpoint.

Fold those into the same verdict cache.

**Done when:**
- A security review accepts the design.
- The guard's existing tests pass unchanged.
- A before/after run on the same pages shows the CPU and wall time saved, with
  identical records and traffic counts.

### TD-24 — Browser reuse and its script cache are tuned on fixtures, not real traffic

**Priority/ownership:** P3; browser traffic and memory. `execution.reuse_browser`
is opt-in, and the numbers behind its defaults come from local fixtures only. As
of 2026-10-09 no Eyebox config enables it, so every browser target still
launches its own driver and browser. Turning it on is the largest
browser-throughput lever available, and it is a config change rather than a
code fix.

**Problem:** three choices rest on fixture measurements:
- The shared-browser replacement threshold, `RECYCLE_AFTER_REQUESTS = 250`
  routed requests, chosen to keep peak memory within about 5% of one browser
  per target.
- The script cache bounds: 32 MiB per run and 8 MiB per response.
- The cache admitting only responses with explicit freshness and no `Vary`
  beyond `Accept-Encoding`.

What the fixtures showed:
- Reuse cut requests by 60–66% and bytes by 82–95%, with identical records.
- Peak memory rose 5–8% in-process and 12–15% in the production image with
  1 MB pages (driver heap timing).

Where it doesn't help:
- Sites that send `no-cache` with `ETag`, `Vary: Origin`, or cache-busting query
  strings get no benefit.
- Heavy pages reach the threshold within one target, so they still launch one
  browser per target.

**Start here:** [browser_pool.py](src/scrapeyard/engine/browser_pool.py),
[asset_cache.py](src/scrapeyard/engine/asset_cache.py),
[browser_session.py](src/scrapeyard/engine/browser_session.py) (`fetch`,
`_ReleasedRoute`), and `run_budget.traffic` (`cached`, `resource_types`).

**Work:**
- Once runs with `reuse_browser` exist, compare each against the same job
  without it: `traffic.requests`, `traffic.bytes`, `traffic.cached`, the
  `script` share of `resource_types`, launches, wall time, and container anon
  peak.
- Only if real pages show a large uncached script share, consider revalidation
  (answering a `304` from the stored body) or `Vary: Origin` support.
- Retune the replacement threshold from real peaks, or count fulfilled bytes as
  well as requests if cached bodies drive the driver's heap.

**Done when:** the defaults are confirmed or changed based on before/after
measurements on real runs with identical records, and README states the
measured effect instead of fixture numbers.

### TD-60 — A browser subrequest can complete during the guard's async check if its frame is detached

**Priority:** P2 · **Area:** engine, browser guard · **Risk:** medium · **Contract:** none · **Depends on:** the TD-23 security review

**Problem:**
- A 2026-10-09 container check saw the rebrowser stealth path send a
  `/favicon.ico` request that was counted differently than expected, making
  `tests/smoke/verify_fetch_sessions.py` flaky at its
  `run_budget.requests == len(Fixture.requests) == 1` assertion (2 of 4 runs
  passed on the old image, 3 of 4 on the new).
- Rechecked 2026-10-09 against real browsers (rebrowser Chromium 136 and
  Playwright Chromium 145): favicon requests and cross-origin
  `<link rel=icon>`, `<img>` and `fetch()` subrequests are all delivered
  through `context.route("**/*")`, so `_guarded_async_intercept_route` sees
  them and aborts a non-public one. The "favicon bypasses the guard"
  hypothesis did not reproduce; favicons are routed and counted.
- One gap remains, and it is a race, not a bypass: because the guard validates
  asynchronously (a thread hop plus any DNS lookup — see TD-23), a request can
  complete before the guard aborts it if the page destroys the request's frame
  during that window. Reproduced with a cross-site iframe removed or navigated
  away mid-check. Same-site iframe removal, popup close and top-level
  navigation did not leak, because those requests share the page's routing and
  are aborted. Shrinking the window (the TD-23 verdict cache) does not close it.
- In every case the host egress policy (iptables `DOCKER-USER`, which denies
  non-public destinations) is the backstop, as it already is for the favicon
  path.

**Code:** `engine/browser_session.py`, `engine/browser_debug.py`
(`_guarded_async_intercept_route`), `tests/smoke/verify_fetch_sessions.py`.
**Fix (needs the security review):** decide whether the frame-detach race is
acceptable given the egress backstop, or close it — for example by taking the
context offline before a frame is torn down, as the fetch-failure path already
does, rather than only aborting in the route handler. Independently, and
without a guard change, make the smoke assertion ignore `/favicon.ico`.
**Done when:** the security review accepts the decision, a test shows a
cross-site frame cannot release a request to a non-public address, and the
smoke verifier passes repeatedly.

## Operational and deployment qualification

These entries need operator work and deployed evidence. They are not single PRs.
For TD-17 to TD-22, read the deployment assessment and the linked source before
implementing.
- Reuse existing scripts and settings.
- Record local implementation checks separately from operator- or
  environment-dependent evidence.
- Keep an entry open until its deployed acceptance checks pass.
- Eyebox's [go-live checklist](../eyebox/GO-LIVE-TESTING-CHECKLIST.md) owns
  closing the launch gates.
- Repository edits alone do not provision infrastructure or activate jobs.
- For consumer-facing work, qualify TD-17's limits together with the explicit
  pagination coverage and removal-scope contract documented in
  [docs/API.md](docs/API.md#pagination-coverage).

### TD-17 — Eyebox and Scrapeyard limits lack a jointly qualified operating envelope

**Priority/ownership:** P2; joint deployment/configuration qualification. The
values below were rechecked on 2026-10-09.

**Locations:**
- Scrapeyard: `src/scrapeyard/common/settings.py` (`run_max_*`,
  `workers_memory_limit_mb`) and `docker-compose.yml` (the `SCRAPEYARD_*`
  defaults).
- Eyebox:
  - `../eyebox/ingest/config.py` (`INGEST_SCRAPEYARD_*`)
  - `../eyebox/docker-compose.yml` and `docker-compose.prod.yml`
  - `../eyebox/.env.prod.example`
  - `../eyebox/.hard-test/scrapeyard-local/environment.json`
  - `../eyebox/docs/deployment-topology.md`

| Limit | Scrapeyard default | Eyebox | Hard-test Scrapeyard env | Consequence to qualify |
| --- | --- | --- | --- | --- |
| Run duration / client wait | 900 s (`run_max_duration_seconds`; Compose default 900) | 2,700 s by default in Eyebox Compose, but 300 s as the code default and in `.env.prod.example` (C-4) | 2,700 s | Under Scrapeyard's Compose defaults, runs longer than 15 minutes fail with `run_duration_seconds` while Eyebox would still be waiting. Real Eyebox runs take 20–25 minutes, and two `midwayusa-known-optics` runs on 2026-10-09 ended at a 1,200 s budget. `execution.deadline_at` can only shorten the budget. |
| Result size | 50 MiB serialized | 10 MiB download | 16 MiB | A successful result between 10 MiB and the server cap is rejected by Eyebox. Include the envelope overhead. |
| Record count | 100,000 | 5,000 listings | 5,000 | Aligned only in the hard-test environment. |
| Memory admission | 4,096 MiB (Compose: 3,072) | — | 0 (disabled) | Hard-test runs never exercise memory admission. |
| Concurrency | 4 workers, 2 browsers | `INGEST_SCHEDULER_MAX_CONCURRENCY` defaults to 2 in production since `a466ee2` | 2 workers, 2 browsers | Qualify two concurrent retailers against browser and memory capacity. |

Eyebox already retries transport failures and keeps the logical run ID for
bounded delayed redispatch (two by default). A polling timeout alone therefore
does not mean work was lost or run twice. Result byte and listing cap violations
are permanent errors, though: retrying the same result can't fix them. Queue
wait, HTTP retry delays, execution and result transfer all add to the end-to-end
time, even though each has its own budget.

**Fix direction:**
- Qualify one operating envelope for the exact launch allow-list: maximum useful
  records and bytes, acceptable queue age, run duration, transfer time, and
  recovery interval.
- First align the existing deployment settings, and split large retailer jobs
  into bounded scopes where that makes sense. This does not need another queue,
  scheduler or shared database.
- Either constrain Scrapeyard's outputs to what Eyebox accepts, or explicitly
  raise Eyebox's limits after memory and processing tests.
- Size the client wait and the total redispatch window from qualified
  queue-plus-run timing.
- Keep source configs stable across a logical retry, and keep Scrapeyard's
  idempotency and result retention longer than the supported recovery window.
- Document the paired settings in both deployment runbooks and verify them
  during preflight. Don't silently raise every limit to its maximum.

**Acceptance checks:**
- Run jobs that take longer than 300 s but finish within the configured server
  budget, including queue delay, a lost submit response, an ingest restart, and
  an interrupted result download.
- Verify one remote job per logical run, and either eventual ingestion or a
  visible bounded failure.
- Test payloads just below and just above the agreed byte and record limits.
  Server and client must behave the same, with no unsafe removal on rejected
  data.
- Record elapsed time and peak memory for the qualified retailer set.
- Extend Eyebox's scheduled-client and source tests and the existing
  cross-provider restart/network-interruption qualification. Mocked unit tests
  alone do not establish the deployed envelope.

**Deployment extension (2026-09-07):** include the actual production resource
and egress configuration.
- `docker-compose.yml` caps Scrapeyard at 4 GiB with a 3,072 MiB admission
  threshold. `docker-compose.qualification.yml` allows 8 GiB and turns off
  untrusted-submission mode for fixtures. Fixture qualification therefore
  doesn't establish production browser capacity or proxy compatibility.
- Measure the frozen launch set on the chosen x86-64 host, with TD-18's forward
  proxy, the intended browser concurrency, and headroom for Redis, the OS,
  images, artifacts, AOF rewrites and temporary backups.
- Record p95/p99 job and queue timing, cgroup memory and CPU, disk and inode
  growth, and failure recovery.
- Coordinate Eyebox load and soak targets with
  [item E](../eyebox/TECH-DEBT.md#e-product-outcomes-capacity-and-recovery-qualification),
  and transport tests with item D.
- Record the measured supported settings and the ongoing cost estimate,
  including proxy bandwidth and retention. The assessment's 4-vCPU/8-GB host
  suggestion and published prices are hypotheses to recheck, not a purchasing
  or capacity guarantee.

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

**Work:**
- Select the smallest suitable existing proxy or service. Document its address
  resolution and connected-destination filtering for HTTP and CONNECT,
  authentication, availability, and secret delivery.
- Wire it through the existing settings and narrowly reviewed host-policy
  exceptions.
- Preserve host egress filtering, startup attestation, and the browser sandbox.
- Ordinary Eyebox credentials keep `submit`/`read` only.
- Supply a repeatable controlled test for public fetch success,
  private/loopback/link-local/metadata denial, redirects, DNS rebinding, and
  proxy failure. Use controlled destinations for the security tests.

**Done when:**
- The production startup path becomes ready with the selected proxy, and fails
  closed when proxy or attestation prerequisites are missing.
- The denial matrix passes from the real container across the enabled HTTP and
  browser transports.
- Retailer yield and timing are qualified through that same proxy under TD-17
  before activation.
- Redacted configuration and version evidence is recorded, and the deployment
  runbook updated.

Trusted local or fixture mode cannot close this item.

### TD-19 — Restricted Hetzner HTTPS ingress and Railway credential handoff are incomplete

**Priority/ownership:** P1; Scrapeyard ingress/security. Coordinate Railway's
outbound-address inventory with [Eyebox item G](../eyebox/TECH-DEBT.md#g-railway-service-configuration-and-ordered-releases).

**Problem:**
- Production Compose exposes no host API port, and the deployment plan has no
  production reverse-proxy configuration.
- Railway's documented static egress now assigns three IPv4 addresses, requires
  Pro, may share addresses between customers, and changes addresses on a region
  move.
- The older single-IP handoff wording needs replacing with the actual assigned
  set. API credentials remain essential.

**Start here:** [ingress and credentials](docs/DEPLOYMENT.md),
[API routes](src/scrapeyard/api/routes.py),
[monitoring authentication](docs/MONITORING.md),
[Hetzner runbook](../eyebox/docs/hetzner-scrapeyard-deployment-runbook.md),
[Railway outbound contract](https://docs.railway.com/networking/static-outbound-ips),
and [Caddy certificate challenges](https://caddyserver.com/docs/automatic-https).

**Work:**
- Provide a reproducible Caddy or Nginx deployment, connected to the private
  Compose network or to a loopback-only production binding.
- Allow the adapter's methods and paths with their needed query parameters:
  `POST /scrape`, `GET /jobs/{id}` and `GET /results/{id}`.
- Preserve the API and idempotency headers, and bound request and transfer
  sizes using TD-17.
- Apply the full verified Railway address set at the network boundary, with a
  named credential restricted to project `eyebox` and `submit`/`read`.
- Keep readiness and metrics on a private monitoring path with separate
  `health-detail` credentials.
- Choose DNS-01 or a deliberately reachable HTTP-01 challenge path, so the
  restrictions don't break certificate renewal.
- Document secret rotation and address-set changes without printing secrets.

**Done when:**
- Deployed ingest can submit, poll and download.
- Disallowed sources, wrong credentials or projects, administrative paths,
  Redis, Docker, and internal monitoring endpoints are all denied externally.
- Idempotency survives the proxy, oversized requests fail predictably, logs
  omit secrets, and certificate issuance and renewal succeed under the final
  firewall policy.
- Both handoff runbooks are updated, and evidence is recorded in Eyebox
  checklist sections 15–17.

### TD-20 — Secure startup after host/Docker restart and runtime monitoring are unqualified

**Priority/ownership:** P1; Scrapeyard host supervision. Coordinate with TD-19's
ingress and TD-22's release artifact selection.

**Problem:**
- `security/deploy-secure-compose.sh` installs policy during a deploy, but no
  checked-in boot service restores it after a host or Docker restart.
- `restart: unless-stopped` neither orders policy restoration nor establishes
  authenticated readiness.
- The current container healthcheck only checks process liveness.

**Start here:** [deploy wrapper](security/deploy-secure-compose.sh),
[egress installer](security/install-docker-egress-policy.sh),
[AppArmor installer](security/install-chromium-apparmor-profile.sh),
[runtime health](src/scrapeyard/main.py), [monitoring](docs/MONITORING.md), and
[installer tests](tests/unit/test_egress_policy_installer.py).

**Work:**
- Add minimal host supervision, normally systemd plus the existing Compose
  scripts. It discovers the current bridge, starts the required peers, installs
  AppArmor and the compatible `DOCKER-USER` policy, and admits work only after
  startup attestation and readiness.
- Handle Docker restarts and network recreation, and bound failed-start
  retries.
- Keep one application process, non-root execution, seccomp, capabilities, and
  filesystem limits.
- Wire private authenticated readiness and metrics collection, with alerts for
  failed background work, queue age, browser failure or saturation, disk and
  inodes, and host or container restart loops.
- Reuse the documented metric names and the chosen monitoring service.
  Coordinate backup-age alerts with TD-21 and Eyebox freshness alerts with
  item E.

**Done when:**
- A cold boot, a Docker restart, and a Compose network recreation each restore
  the intended controls and exactly one instance automatically.
- With policy, probe or Redis failures injected, no unprotected work starts and
  retries stay bounded.
- A real readiness, queue or disk alert and a no-data condition both reach the
  operator and recover.
- Redacted effective-policy, resource, readiness and reboot evidence is kept.

A plain `docker compose up` is not enough.

### TD-21 — Unattended coordinated off-host backup and restoration are incomplete

**Priority/ownership:** P1; Scrapeyard recovery. Eyebox owns its PostgreSQL
recovery in [item I](../eyebox/TECH-DEBT.md#i-railway-postgresql-recovery-and-scheduled-maintenance).

**Problem:**
- The repository has a tested helper for quiesced backup sets, but no complete
  unattended production backup, upload, retention and alerting setup.
- The helper excludes Redis and encryption key material.
- Independent snapshots of live SQLite files, artifacts and Redis are not a
  coordinated recovery set.
- TD-48 drives the size of the backup.

**Start here:** [backup helper](scripts/qualification_backup.py),
[required quiescing order](docs/TESTING.md#quiesced-backup-and-fresh-restore),
[encryption recovery](docs/SECRET_STORAGE.md#rotation-backup-restore-and-key-loss),
and [release qualification](scripts/run_release_qualification.sh).

**Work:**
- Wrap the existing helper in a scheduled operation:
  1. Pause ingress and new dispatch.
  2. Quiesce application writers.
  3. Follow the documented Redis persistence steps.
  4. Capture all three databases and the result and adaptive files.
  5. Validate the manifest.
  6. Encrypt and upload off-host.
  7. Resume safely.
- Define retention, failure cleanup, disk headroom, backup age, and operator
  notification.
- Store keyring recovery material separately, with tested access.
- State whether queued work is preserved or recovered/reconciled, and package
  the corresponding Redis state explicitly.
- Hetzner server backups are supplemental. They exclude attached provider
  Volumes, while Docker volumes on the root disk belong to that disk.

**Done when:**
- An unattended scheduled backup succeeds.
- Upload or storage failures and stale-backup alerts reach the operator, and the
  service recovers safely from an interrupted backup.
- Off-host data, keys, and the chosen Redis recovery state restore into an
  isolated deployment.
- Known successful and failed jobs, readable results, decrypted configuration,
  and queued-work recovery are verified there, with no unintended external
  submissions or webhooks.
- Measured RPO/RTO, object checksums, retention, and an operator-executable
  recovery procedure are recorded.

A manifest-only check or a provider disk snapshot does not close this item.

### TD-22 — Deployment and rollback lack retained, qualified release artifacts

**Priority/ownership:** P2; Scrapeyard release delivery. Integrates with TD-20;
coordinate joint release IDs with Eyebox item G. TD-48 would add an on-disk
format change to the rollback story.

**Implementation update (2026-09-07):**
- [`scripts/release_artifact.py`](scripts/release_artifact.py) now builds a
  committed revision, gates publication on the existing scan and quick
  qualification, and keeps source and security files, reports, and all three
  runtime images in a checksummed bundle.
- The secure wrapper's `--release DIRECTORY` path verifies and loads that bundle
  before stopping the app, preserves the policy/attestation ordering, and
  disables builds and pulls.
- The [rollback procedure](docs/DEPLOYMENT.md#retained-release-artifacts-and-rollback)
  covers keeping the previous release, the key and backup prerequisites, and
  schema compatibility.
- Local validation:
  - Ruff and mypy passed.
  - The full suite passed 2,084 tests, with 16 live Redis skips (88.51%
    coverage).
  - The expanded release, wrapper and CI checks passed 22 tests with warnings
    treated as errors.
- Artifact creation was attempted locally and correctly refused before
  building, because `scrapeyard_backend` already owns the qualification lane's
  fixed `172.30.0.0/24` subnet.
- No qualified candidate was published, and no running deployment was changed.

**Still open:** prepare the current and previous artifacts on an isolated
Docker host, then complete the deployed acceptance checks below and record the
evidence before removing this entry.

**Problem:**
- The secure wrapper rebuilds Scrapeyard from the checkout on every deployment.
- CI builds and scans images, but the checked-in workflow doesn't provide a
  retained production image or a tested rollback path that is independent of a
  new dependency or browser download.
- Rebuilding an older revision doesn't prove that the previously qualified
  artifact is available during an incident.

**Start here:** [Dockerfile](Dockerfile),
[container-security workflow](.github/workflows/container-security.yml),
[security scan](scripts/run_container_security_scan.sh),
[deploy wrapper](security/deploy-secure-compose.sh), and
[qualification runner](scripts/run_release_qualification.sh).

**Work:**
- Keep the built, scanned and qualified image by immutable digest or verified
  archive, together with its source revision and the matching Compose and
  security configuration.
- Add the smallest explicit promotion and rollback path that uses that artifact
  without rebuilding, and preserves the stop, dependency, policy and attestation
  ordering.
- Keep a previous known-good artifact, and enough local disk to restore it.
- Record the pre-release backup and key requirements and SQLite migration
  compatibility. When an older binary can't use the new schema, choose between
  a data restore and a forward repair.
- Preserve the existing local build workflow where it is useful.

**Done when:**
- The retained candidate is deployed, a failed qualification is rejected, and
  the previous compatible release is restored without build-network access.
- Readiness, known result reads, the effective security policy, and a single
  application instance are verified after both transitions.
- If schema rollback requires it, the documented data-restore branch is
  exercised in isolation.
- Image and config identifiers and the commands used are recorded, so another
  operator can repeat the rollback.

## Deferred

These were considered on 2026-10-09 and deliberately not scheduled. Each one
records why and what would change that.

### TD-48 — Stored configs are duplicated and uncompressed

**Priority:** P3 · **Status:** deferred · **Contract:** changes the on-disk format and so affects rollback (TD-22)

**Problem:**
- `config_yaml` is stored in both `jobs` and `job_runs`
  (`SQLiteJobStore._insert_job`, `claim_run`). Live encrypted configs average
  75 KB, with a maximum of 339 KB.
- 136 jobs already make jobs.db 21 MB; at 30 days of Eyebox volume it is
  projected at about 1 GB.
- `config_yaml` is the 5th column, so reading any later column walks the blob's
  overflow chain. A `current_run_id` lookup takes 96 ms against 4 ms for
  `status`, and `list_jobs_with_stats` takes 95 ms per page.
- Eyebox configs compress about 9× with zlib: 1 GB would become 119 MB, and
  those queries would take 9–12 ms.
- This drives TD-21's backup size. It also drove startup memory until the
  secret migration started paging through rows (2026-10-09).

**Why deferred:**
- Compressing before encrypting (a `syenc:v2` envelope read alongside v1)
  changes the on-disk format, so an older binary couldn't read new rows. That
  breaks the TD-22 rollback story.
- Compression before encryption of attacker-influenced plaintext needs a
  security review.
- The startup-memory symptom is already gone without a format change: the
  secret migration pages through rows.

**Revisit when:** jobs.db grows past a few hundred MB, or TD-22's rollback
process covers format migrations. A cheaper intermediate step is moving the
configs to a side table, which still needs a migration but involves no crypto
change.

### TD-49 — Config and engine features Eyebox doesn't use

**Priority:** P3 · **Status:** deferred · **Contract:** the schema is `extra="forbid"`, so removing a key is a breaking change

**Unused by Eyebox as of 2026-10-09:**
- **Execution and jobs:**
  - adaptive matching (`adaptive: true`); `adaptive_domain` appears on 11
    Brownells targets but does nothing
  - `execution.reuse_browser` with `BrowserPool` and `AssetCache` (TD-24)
  - job- and target-level `proxy`
  - `execution.mode: sync|auto`, `priority: high|low`,
    `fail_strategy: continue`
  - `execution.domain_daily_page_limit` (Eyebox sets the service-wide env var
    instead)
  - `page_cache: replay`
- **Browser:**
  - options `cdp_url`, `nstbrowser_mode`, `block_url_patterns` and
    `additional_arguments`
  - action types `scroll`, `repeat_click` and `wait_ms`
- **Pagination and retry:**
  - `pagination.page_first` and `page_step`
  - `retry.backoff: linear|fixed`
  - `on_empty: retry|skip`
- **Output and delivery:**
  - `output.group_by: target`
  - `schedule.timezone`
  - `webhook.headers` and `timeout`
- **Detection:** stock `text_patterns`, `limited_stock` and `preorder`.
- **Transforms:** `lowercase`, `uppercase`, `append`, `remove`, `strip_suffix`,
  `extract`, `default` and `replace`.

**Keep:**
- the price-check passthrough keys listed in the
  [contract](#eyebox-contract)
- the single `target:` form, which Eyebox's cold-start probe uses

**Why deferred:** removing them gains little (extraction already takes 12 ms or
less per page), it risks breaking any config that adopts them later, and
Scrapeyard is a general service. Revisit only if one of these features blocks
other work. If trimming, deprecate first with a validation warning, then
remove.

## Consumer-side follow-ups (fix in Eyebox)

These were found while mapping the contract. They are fixed in `../eyebox`, not
here; record them in Eyebox's tracker when picked up.

| ID | Finding | Where (Eyebox) |
| --- | --- | --- |
| C-1 | `POST /scrape` is sent with `retry=False`. A busy 503 (`Retry-After: 5`) falls through to `/scrape/acceptance`, gets a 404, and the run fails. Price check marks the result `UNAVAILABLE`. Retrying is safe, because the same key and bytes replay. | `ingest/scrapeyard_run.py` (submit), `pipeline/price_check.py`, `pipeline/offer_accuracy.py` |
| C-2 | The alias backfill's `DELETE /domains/{host}/guard` needs `transport-admin` and an unscoped caller, so it gets a 403 that is never checked. | `ingest/pdp_alias_backfill.py` |
| C-3 | `SCRAPEYARD_HEALTH_PROBE_API_KEY` and `SCRAPEYARD_LOCAL_DEVELOPMENT_UNAUTHENTICATED` are set but Scrapeyard reads neither. The smoke tool parses `dependencies.*` from a 503 on `/health`, but `/health` is now public liveness (`{"status":"ok"}`) and the detail lives at authenticated `/health/ready`. | `scripts/hard-test.sh`, `scripts/hard-test-cold-start.sh`, `ingest/scrapejob_smoke.py` |
| C-4 | The wait-budget defaults disagree: the code and `.env.prod.example` use 300 s, while Compose defaults to 2,700 s. An operator who copies `.env.prod.example` overrides Compose. This feeds TD-17. | `ingest/config.py`, `.env.prod.example` |
| C-5 | The hard-test Scrapeyard container runs without an init process (`Init=<nil>` on 2026-10-09), even though `DEPLOY.md` requires one. Launch it with `--init`. Optionally, Scrapeyard could warn at startup when it runs as PID 1. | `.hard-test/scrapeyard-local/` launcher |
| C-6 | The docs disagree on webhooks. `DEPLOY.md` says Scrapeyard signs `X-Webhook-Signature`; it doesn't, as `README.md` correctly says. `AGENTS.md` and `README.md` say ingest "receives Scrapeyard webhooks", but production pulls. 51 configs still carry `webhook` blocks. `make scrape-live` without strip sends them, and Scrapeyard's dispatch-time SSRF check blocks `host.docker.internal`. | `DEPLOY.md`, `README.md`, `AGENTS.md`, `configs/` |
| C-7 | 11 configs list 403 in `retryable_status`, which Scrapeyard never retries. `adaptive_domain` on 11 Brownells targets does nothing with `adaptive: false`. Both are cosmetic; keep the keys valid. | `configs/` |
| C-8 | Target summary URLs have their query values redacted, so exact URL matching fails for 119 query-string targets across 8 configs, and diagnostics and the backfill fall back to position. This works today; don't add exact-URL logic without a Scrapeyard change. | `ingest/scrapejob_diagnostics.py`, `ingest/pdp_alias_backfill.py` |
| C-9 | The docs disagree on the pinned production Scrapeyard revision: `ce8fc04` vs `5de7dc9`+. `scrapeyard_identity.source_run()` (legacy `runs[]` shape) is used only by tests. | `docs/hetzner-scrapeyard-deployment-runbook.md`, `docs/scrapeyard-work-orders.md`, `ingest/scrapeyard_identity.py` |
