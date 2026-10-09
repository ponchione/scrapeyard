# Technical debt and PR roadmap

This is Scrapeyard's working backlog. Unless an entry says otherwise, **each
open entry is one pull request**, and its lettered sub-items are commits within
it. Read [How to use this list](#how-to-use-this-list) and the
[Eyebox contract](#eyebox-contract) before starting any entry.

**Audit history:**
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
   poetry run ruff check src tests scripts
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
7. **New findings** take the next free ID (**TD-58**) and use this template:

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
| `ruff format --check` | 160 files would change; the formatter is not adopted (TD-44) |
| GitHub Actions | Disabled for the repository. The last runs on `main` (2026-09-07) failed in CI, Dependency audit, and Container security (TD-26). |
| `pip-audit` on pip 26.1.2 | PYSEC-2026-3721, fixed in 26.2 (TD-26) |
| Eyebox configs vs. schema | 56 of 56 validate |
| `pytest -W error` after TD-25 | 2,356 passed, 17 skipped, with no env override and no network lookups; 41–54 s |

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
- `POST /jobs/{id}/cancel?run_id=` must return 204 (see TD-47).
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
| 1 | TD-39 | P3 | click pagination | medium | timing only | |
| 2 | TD-40 | P3 | API | low | `offset` bound | |
| 3 | TD-41 | P3 | dead code, duplication | low | none | Before TD-42 |
| 4 | TD-42 | P3 | `job_store.py` split | low | none | Last of the storage work |
| 5 | TD-43 | P3 | image, dependencies | low | none | |
| 6 | TD-44 | P3 | lint, docs | low | none | |
| 7 | TD-51 | P3 | lifespan | medium | none | Needs a design decision |
| 8 | TD-52 | P2 | worker | low | none | |
| 9 | TD-53 | P3 | engine | low | none | |
| 10 | TD-54 | P3 | config validation, API | low | new 422s for configs that already break | |
| 11 | TD-55 | P2 | URL guard, API | low | none | |
| 12 | TD-56 | P3 | redaction | low | none | |
| 13 | TD-57 | P3 | storage | low | none | |

**Before the first deploy:** TD-26 (CI and the pip bump). CI is GitHub Actions,
not deployment, so local checks cover it until then. The pip vulnerability only
matters in a built image, and a release (TD-22) needs the container scan and
qualification workflows passing.

**Coordinated:**
- TD-45: needs Eyebox to move with it.
- TD-23 and TD-46: one security review covering both.
- TD-47: a decision.
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
| TD-26 | P1 | before first deploy | CI is disabled and red, and the pinned pip has a known vulnerability |
| TD-39 | P3 | ready | Click pagination keeps every parse tree and repeats the full wait per click |
| TD-40 | P3 | ready | API edge cases: unbounded offsets, unlogged failures, uncounted response memory |
| TD-41 | P3 | ready | Dead code and duplicated helpers |
| TD-42 | P3 | ready | `job_store.py` (2.5k lines) mixes five concerns |
| TD-43 | P3 | ready | The runtime image and dependency declarations carry waste |
| TD-44 | P3 | ready | Lint configuration is partly inert, and README omits settings |
| TD-45 | P1 | coordinated | Bundled browsers are months behind upstream |
| TD-46 | P2 | coordinated | Subrequest DNS failures and IPv6 gaps in the browser guard |
| TD-47 | P2 | coordinated | Eyebox cannot cancel its own runs: cancel requires the `delete` scope |
| TD-48 | P3 | deferred | Stored configs are duplicated and uncompressed |
| TD-49 | P3 | deferred | Config and engine features Eyebox doesn't use |
| TD-51 | P3 | ready | Shutdown ends in SIGKILL while a run or result thread is still active |
| TD-52 | P2 | ready | A cancellation during run-budget handling skips cancellation recovery |
| TD-53 | P3 | ready | On Python 3.10, browser navigation timeouts are misclassified |
| TD-54 | P3 | ready | A lone surrogate in a selector field name makes that run's `GET /results` fail with 500 |
| TD-55 | P2 | ready | URL redaction in text is quadratic on long URL-like tokens |
| TD-56 | P3 | ready | Secret redaction order depends on the process hash seed for equal-length secrets |
| TD-57 | P3 | ready | Stale-running recovery scans every job under the write lock |

## Ready to implement

### TD-26 — CI is disabled and red, and the pinned pip has a known vulnerability

**Priority:** P1 · **Area:** CI, build · **Risk:** low · **Contract:** none · **Depends on:** an operator re-enabling Actions

**Problem:** Actions is disabled for the repository
(`gh api repos/{owner}/{repo}/actions/permissions` returns `enabled: false`).
The last runs on `main` (2026-09-07) failed in CI, Dependency audit and Container
security, so nothing gates merges.
- **a. pip 26.1.2 has PYSEC-2026-3721** (fixed in 26.2) and ships in the runtime
  image. It is pinned in:
  - `Dockerfile` (`ARG PIP_VERSION`, both stages)
  - `.github/workflows/ci.yml` (`PIP_VERSION`)
  - `.github/workflows/dependency-audit.yml`
  - `scripts/check_packaging_toolchain.py` (`PIP_VERSION`)
  - literal strings in `tests/unit/test_packaging_toolchain.py` and
    `tests/unit/test_dependency_audit_script.py`
- **b. The `live-redis` job cannot pass.**
  `tests/live_redis/test_queue_lifecycle.py` asserts `appendonly yes`, but
  GitHub service containers can't pass `--appendonly yes`.
- **c. The container scan fails,** which is consistent with the stale Debian
  snapshot (`ARG DEBIAN_SNAPSHOT=20260715T000000Z`).
- **d. CI wastes work** (optional; same PR or a follow-up):
  - mypy targets 3.10 on both matrix legs, so the second run repeats the first.
  - A PR that only touches `src/` does two cold image builds of about 2.5 min
    each.
  - The browser-smoke path filter leaves out `src/scrapeyard/models/**` and
    `sql/**`.

**Fix:**
- **a.**
  - Bump pip to 26.2 or later everywhere.
  - Have the tests import the constant from
    `scripts/check_packaging_toolchain.py` instead of repeating the literal.
  - Remove pip from the runtime stage after the final install
    (`pip uninstall -y pip`), after first confirming nothing at runtime invokes
    it.
- **b.** Add a step:
  `docker exec ${{ job.services.redis.id }} redis-cli CONFIG SET appendonly yes`.
- **c.** Advance the snapshot and re-run the scan.
- **d.** Run mypy on one leg, share or cache the image layers between jobs, and
  widen the path filter.

**Done when:** an operator has re-enabled Actions, every workflow passes on
`main`, and `pip-audit` is clean.

### TD-39 — Click pagination keeps every parse tree and repeats the full wait per click

**Priority:** P3 · **Area:** engine, pagination · **Risk:** medium · **Contract:** timing only; records must be identical · **Depends on:** —

- **a. Every page's parse tree is kept.**
  - `BrowserSession._click_through_pages` and `_extract_click_pages` parse every
    click snapshot and keep it until the target ends, while the browser is
    still open.
  - Measured about 5.6 MB per eurooptic page (1.42 MB of HTML).
    `eurooptic-riflescopes` uses `max_pages: 70` (its byte budget was raised to
    256 MiB for this), which is about 390 MB of trees at once.
  - Parsing, about 6 ms a page, runs on the event loop.
  - **Fix:** keep the raw HTML (optionally compressed), parse each snapshot in a
    worker thread, and drop it after extraction. Record/replay builds the same
    structure, so update both.
- **b. The full wait runs after every click.**
  - `_click_through_pages` waits the full `wait_ms` after every click, even
    once the item change is confirmed.
  - eurooptic's 1,000 ms wait adds up to 69 s per riflescopes run; the
    eurooptic-optics job took 932 s.
  - **Fix:** skip or cap the wait once the change is confirmed.

**Done when:** records are identical on eurooptic page-cache recordings, with
peak memory and wall time recorded before and after.

### TD-40 — API edge cases: unbounded offsets, unlogged failures, uncounted response memory

**Priority:** P3 · **Area:** API · **Risk:** low (a, b), medium (c) · **Contract:** sub-item a only, on `GET /jobs` and `/errors`, which Eyebox doesn't use · **Depends on:** —

- **a. Huge offsets cause a 500.** `?offset=9223372036854775808` on `list_jobs`
  or `get_errors` makes SQLite raise `OverflowError`, which becomes a 500.
  **Fix:** add `le=` to the `Query` so these return 422.
- **b. Some failures are returned without being logged.**
  - In `_apply_scheduler_mutation`, `create_job` and `trigger_job`, a successful
    compensation or rollback returns 503 but never logs the original exception.
  - `trigger_job`'s `except Exception` swallows everything, including
    `SecretKeyConfigurationError`, which has its own handler.
  - **Fix:** log `error_type` before raising, and narrow the catch.
- **c. Response memory outlives the result slot.**
  - `get_results` releases its result slot when the handler returns, but the
    rendered body (up to 100 MiB) stays in memory while slow clients download
    it. That memory isn't counted against `api_result_thread_max_workers`.
  - **Fix:** hold the slot until the body has been sent (a small `Response`
    subclass), keeping `Content-Length`.

### TD-41 — Dead code and duplicated helpers

**Priority:** P3 · **Area:** mixed · **Risk:** low (a is medium because tests need rewriting) · **Contract:** none · **Depends on:** do before TD-42

Everything here was grepped across `src/` and `tests/`. Grep again before
deleting.

- **a. The legacy non-session browser fetch path.**
  - `_prepare_scrape_context` always wraps fetchers in sessions, so production
    never reaches these:
    - the module-level Scrapling monkeypatch `_install_browser_route_guard()`
    - `browser_debug._close_page_safely`
    - the non-session branches of `fetch_basic_response`,
      `fetch_browser_response`, `scraper._measured_response_body_bytes` and
      `_fetch_basic_with_safe_redirects`
  - **`_guarded_async_intercept_route` itself is live and must stay.** The
    session path passes it to `BrowserSession` as `route_handler`.
  - Keep `_uncache_response_encoding`.
  - **Confirm with coverage.** The legacy path is driven by tests that call
    `fetch_browser_response` without a session and rely on the patched
    `scrapling_pw_engine.async_intercept_route`, at least
    `test_subrequest_filter.py` and `test_browser_debug_observability.py`. Run
    the suite without them, delete only the lines that become uncovered, and
    rewrite those tests onto the session path.
  - The remaining Scrapling coupling is the name-mangled private calls in
    `browser_session.py`. That is fine while the version is pinned below 0.3;
    0.2.99 is the last 0.2 release.
- **b. A risky default.** `submit_scrape_job(load_result=True)` and
  `ScrapeSubmission.results` are used only by tests; the route always passes
  `False`. The default of `True` would load a 50 MiB artifact without the
  result pool's concurrency limit. Remove the parameter, or default it to
  `False`.
- **c. Used only by tests or never imported:**
  - `RunBudget.consume_extracted_records`
  - `SQLiteErrorStore.delete_errors_for_run` and its `ErrorStore` protocol
    method (no callers at all)
  - `WebhookOutboxStore.enqueue_delivery`, `get_delivery` and `list_pending`
  - `filesystem.read_json_file` and `write_json_file`
  - the re-exports in `webhook/__init__.py`
- **d. Decrypt-then-verify is written out six times.** The pattern appears in
  `job_store.py` in `get_job_run_state`, `get_queued_run_config` (twice),
  `_insert_failed_queued_run`, `claim_run` and `queue_run`. Add one
  `_reveal_verified(value, purpose, expected_hash)` helper.
- **e. Two copies of the same thread pool.** `api/result_responses.py`
  (`ResultResponseThreadPool`) and `common/run_threads.py` (`RunThreadPool`)
  duplicate the slot, lingering-thread and metrics logic. Share a base class
  parameterized by the metrics label. This one is medium risk.
- **f. Duplicated cleanup call.** `storage/cleanup.py`
  (`start_cleanup_loop._loop`) repeats the whole `run_cleanup` call in two
  branches that differ only in the webhook arguments. Its `getattr(settings, …)`
  fallbacks cover settings fields that always exist.
- **g. Small lint findings.** Two `TRY203` handlers that only re-raise in
  `scheduler/cron.py`, an unused `noqa` in `common/logging.py`, and a silent
  `try`/`except`/`pass` in `browser_debug.py` (log it).

### TD-42 — `job_store.py` (2.5k lines) mixes five concerns

**Priority:** P3 · **Area:** storage · **Risk:** low (mechanical) · **Contract:** none · **Depends on:** TD-41 (to avoid conflicts)

**Fix:** keep `SQLiteJobStore` as a thin facade and move the code into module
functions that take `db`. Approximate line ranges at `b0b285e`:

| Concern | Lines | Starting from |
| --- | --- | --- |
| Retention | ~391–659 | `summarize_cleanup_backlog` |
| Deletion and cancel | ~857–1185 | — |
| Terminal webhook reconciliation | ~214–286 and ~1828–2097 | — |
| Queued/stale recovery | ~2213–2510 | — |

These are pure moves with no behavior change, one commit per module, and the
existing tests pass unchanged.

### TD-43 — The runtime image and dependency declarations carry waste

**Priority:** P3 · **Area:** Dockerfile, `pyproject.toml` · **Risk:** low · **Contract:** none; Eyebox's dev container uses its own venv and browser cache · **Depends on:** —

- **a. About 570 MB of unused Chromium headless shells.**
  - The image installs Chromium headless shells (about 570 MB uncompressed)
    that are never used. Scrapling launches full Chromium
    (`channel='chromium'`), and `BrowserSession` is the only launch site.
  - **Fix:** add `--no-shell` to the two `playwright install` lines in the
    Dockerfile, and update `tests/unit/test_browser_runtime_contract.py`.
- **b. Build packages that are never needed.**
  - `build-essential`, `libxml2-dev` and `libxslt1-dev` are installed from the
    slow snapshot mirror and purged later. Every Linux dependency in the lock
    ships a cp312 x86_64 wheel.
  - **Fix:** drop them and add `--only-binary=:all:` to the pip install.
- **c. Direct imports that aren't declared** in `pyproject.toml`: playwright,
  rebrowser-playwright, camoufox, redis, tldextract and starlette.
  - Scrapling only requires `playwright>=1.49.1` and `camoufox>=0.4.11`, while
    the Dockerfile asserts exact versions.
  - **Fix:** declare pins equal to the lock, so no installed version changes.
- **d. `.dockerignore` gaps.** It doesn't exclude `artifacts/`, `.artifacts/`,
  `.worktrees/`, `data/` or `*.db`. `.worktrees/` matters once the worktree
  workflow in [How to use this list](#how-to-use-this-list) is followed.

**Done when:** the image builds, browser smoke passes, and the image size is
recorded before and after.

### TD-44 — Lint configuration is partly inert, and README omits settings

**Priority:** P3 · **Area:** tooling, docs · **Risk:** low · **Contract:** none · **Depends on:** —

- **a. Import sorting is switched off.** `"I"` is selected but `I001` is
  ignored, so import sorting is dead config with 110 hidden violations. Ruff is
  pinned at `^0.9` (0.9.10), and `security/*.py` isn't linted (CI runs
  `ruff check src tests scripts`). **Fix:** bump ruff, enable `I001` with
  `--fix`, and add `security`.
- **b. `ruff format` isn't adopted.** It would rewrite 160 files, about 4.6k
  lines (58 of them `src` files, about 1.25k lines). Either adopt it together
  with (a), in its own commit listed in `.git-blame-ignore-revs` and with
  `ruff format --check` in CI, or decide not to and delete this sub-item.
- **c. The extended rule scan is informational only** (`ruff check --select
  PERF,RUF,PL,ASYNC,PTH,RET,TRY,S src`). The findings worth acting on are in
  TD-41g. Two kinds of hit are expected and should be left alone:
  - `S506` on `ScrapeyardSafeLoader` is a false positive, because it subclasses
    `yaml.SafeLoader`.
  - `PLC0414` on `import x as x` marks intentional re-exports for mypy.
- **d. The README settings table is incomplete.** It omits
  `WORKERS_MEMORY_LIMIT_MB` and 17 other settings. **Fix:** fill it in, and add
  a test that checks the table against `ServiceSettings`.

### TD-51 — Shutdown ends in SIGKILL while a run or result thread is still active

**Priority:** P3 · **Area:** lifespan · **Risk:** medium · **Contract:** none · **Depends on:** —

**Problem:**
- `_shutdown_runtime_services` keeps every shared store open while any owned
  phase is still live. That includes an active run thread or result-response
  thread, even after the worker pool has stopped cleanly.
- aiosqlite's connection threads are non-daemon, so the interpreter waits for
  them at exit and Compose kills the container at its 40 s `stop_grace_period`.
- This is the documented trade-off: unresolved work "remains tracked and the
  runtime reports shutdown failure instead of silently abandoning it", and two
  tests enforce it (`test_shutdown_retains_shared_services_for_unresolved_worker`,
  `test_lifespan_retains_instance_guard_for_unresolved_worker`). SQLite stays
  consistent, but every such shutdown ends in SIGKILL.
- `WorkerPool.stop` keeps this design. Once the pool has used its
  cancellation reserve, no grace is left to close the stores anyway.

**Code:** `main.py` (`_shutdown_runtime_services`, `live_owned_phases`,
`retain_shared_services`).
**Fix:** decide whether a thread that ignores cancellation should keep the
stores open. Either close the stores once only lingering threads remain, or
give the thread pools a checkpoint that stops their threads before exit, and
update the two tests and `docs/DEPLOYMENT.md` to match.
**Done when:** a test with a stuck run thread shows shutdown closing the stores
and the process exiting within the grace, or the trade-off is accepted and this
entry deleted.

### TD-52 — A cancellation during run-budget handling skips cancellation recovery

**Priority:** P2 · **Area:** worker · **Risk:** low · **Contract:** none · **Depends on:** —

**Problem:**
- `_handle_budget_exhaustion` runs inside `scrape_task`'s
  `except BudgetExceeded` block. A `CancelledError` raised there, for example
  a shutdown cancel during its database writes, propagates out without
  reaching the sibling `except asyncio.CancelledError` handler.
- The run then stays `running` until the heartbeat timeout, the symptom the
  shutdown cancellation reserve fixed for the main path. An `Exception` from
  the same call already goes through crash handling, but `CancelledError` is a
  `BaseException`.

**Code:** `queue/worker.py` (`scrape_task`, `_handle_budget_exhaustion`).
**Fix:** send a cancellation from the budget path through the same ownership
check and shielded crash finalization as the main cancellation handler.
**Done when:** a test that cancels the task during budget finalization shows
the run reaching `failed` or `cancelled` before the task ends.

### TD-53 — On Python 3.10, browser navigation timeouts are misclassified

**Priority:** P3 · **Area:** engine · **Risk:** low · **Contract:** none; production and Eyebox's container run 3.12, which is unaffected · **Depends on:** —

**Problem:**
- `BrowserTransportTimeout` subclasses the builtin `TimeoutError`, but
  `classify_fetch_exception` checks `asyncio.TimeoutError | httpx.TimeoutException`.
- Before 3.11, `asyncio.TimeoutError` is a separate class, so on 3.10 the check
  misses. The builtin `TimeoutError` is an `OSError`, so a browser navigation
  timeout ends as `browser_error` or `network_error` instead of
  `navigation_timeout`.
- `pyproject.toml` allows 3.10 and CI tests it. Not reproduced on a 3.10
  interpreter. Egress attestation had the same bug and now catches both
  classes.

**Code:** `engine/browser_debug.py` (`BrowserTransportTimeout`),
`engine/fetch_classifier.py` (`classify_fetch_exception`).
**Fix:** also match the builtin `TimeoutError` (the same class from 3.11 on),
and grep for other places that catch only one of the two.
**Done when:** a test raising `BrowserTransportTimeout` through
`classify_fetch_exception` expects `navigation_timeout`, and it runs on 3.10 in
CI.

### TD-54 — A lone surrogate in a selector field name makes that run's `GET /results` fail with 500

**Priority:** P3 · **Area:** config validation, API · **Risk:** low · **Contract:** new 422s only for configs that already break; no Eyebox config contains a surrogate · **Depends on:** —

**Problem:**
- YAML `"\ud800"` loads as a lone surrogate. Used as a `selectors` key, it
  reaches records and `targets[].debug.selector_counts`.
- The run ends `complete` and the artifact writes, ASCII-escaped. But
  `_render_result_response` encodes with `ensure_ascii=False`, so every
  `GET /results` for that run raises `UnicodeEncodeError` and returns a generic
  500. Reproduced on `a36c0a7` through `POST /scrape` and the real worker.
- Lone surrogates in transform arguments, selector queries or target URLs fail
  the target inside the engine instead, reported as `error_type: http_error`.
- Page content can't produce one: basic fetch decodes with `errors="replace"`,
  Chromium delivers U+FFFD, and a server-declared `unicode_escape` or `utf-7`
  charset fails in Scrapling's `Adaptor`, also as `http_error`.

**Code:** `config/schema.py`, `config/loader.py`, `api/result_responses.py`
(`_render_result_response`), `engine/fetch_classifier.py`
(`classify_fetch_exception`).
**Fix:** reject strings containing U+D800–U+DFFF anywhere in a submitted config
with 422. Optionally, classify a `UnicodeError` raised while parsing as a
content error instead of `http_error`.
**Done when:** a config with a lone surrogate in a field name, transform
argument, selector or URL gets a 422, and a test shows no run can complete
with a record that `/results` cannot encode.

### TD-55 — URL redaction in text is quadratic on long URL-like tokens

**Priority:** P2 · **Area:** engine URL guard, API · **Risk:** low · **Contract:** none · **Depends on:** —

**Problem:**
- `_URL_IN_TEXT_RE` (`[a-z][a-z0-9+.-]*://[^\s"']+`) backtracks from every
  start position across a long run of `[a-z0-9+.-]` that contains no `://`.
- Measured on `main`: 4,000 characters take 33 ms, 8,000 take 127 ms and
  16,000 take 0.51 s. A 256 KiB config, within
  `SCRAPEYARD_MAX_REQUEST_BYTES`, holding one such token would cost about two
  minutes of CPU per call, holding the GIL.
- `redact_userinfo_in_text` has 19 call sites, including YAML and validation
  error text, error records and `GET /jobs/{id}` config redaction, so one
  authenticated submission could stall the service.

**Code:** `engine/url_guard.py` (`_URL_IN_TEXT_RE`, `redact_userinfo_in_text`).
**Fix:** bound the scheme (`[a-z][a-z0-9+.-]{0,31}://`) or anchor it at a word
boundary, keeping today's matches.
**Done when:** a test redacts a 256 KiB token in well under 100 ms, and the
existing redaction tests pass unchanged.

### TD-56 — Secret redaction order depends on the process hash seed for equal-length secrets

**Priority:** P3 · **Area:** redaction · **Risk:** low · **Contract:** none · **Depends on:** —

**Problem:**
- `_variants_longest_first` sorts secret variants by length only, so
  equal-length variants come out in set order, which depends on the
  per-process string hash seed.
- When two equal-length secrets overlap in a string (secrets `ab` and `ba` in
  `abab`), the redacted text differs between processes: either
  `<redacted><redacted>` or `a<redacted>b`, and the latter keeps fragments of
  a secret. This needs overlapping equal-length secrets, so it is contrived.

**Code:** `engine/url_guard.py` (`_variants_longest_first`).
**Fix:** sort by `(-len(variant), variant)`, or merge overlapping matches into
one redaction span.
**Done when:** a test with overlapping equal-length secrets gives the same
output under different `PYTHONHASHSEED` values.

### TD-57 — Stale-running recovery scans every job under the write lock

**Priority:** P3 · **Area:** storage · **Risk:** low · **Contract:** none · **Depends on:** —

**Problem:**
- `recover_stale_running_jobs` runs inside `BEGIN IMMEDIATE` every
  `workers_running_reconciliation_interval_seconds` (60 s) and plans as
  `SCAN jobs`: 5.3 ms at 6,600 jobs and 16.7 ms at 25,000, on the shared
  jobs.db connection.
- It can't use the partial `idx_jobs_admission`, because it deliberately
  includes `running` rows whose `current_run_id` is NULL.

**Code:** `storage/job_store.py` (`recover_stale_running_jobs`).
**Fix:** split it into a `UNION`: an indexed branch for running rows with a
`current_run_id`, plus a branch for running rows without one, backed by a small
partial index (for example `(job_id) WHERE status = 'running' AND
current_run_id IS NULL`) or by proof that the state can't exist.
**Done when:** the plan has no `SCAN jobs`, and results are unchanged on seeded
edge rows.

## Coordinated

These need a decision, a security review, an Eyebox change, or real-traffic
data before any code.

### TD-45 — Bundled browsers are months behind upstream

**Priority:** P1 · **Area:** dependencies, browsers · **Risk:** medium · **Contract:** Eyebox's hard-test venv (`../eyebox/.hard-test/scrapeyard-venv`) and the host's `~/.cache/ms-playwright` must move in lockstep · **Depends on:** coordination with Eyebox

**Problem:**
- rebrowser-playwright 1.52 ships Chromium 136.0.7103, which the dynamic and
  stealthy fetchers use. Playwright 1.58 ships Chromium 145.
- Scrapeyard loads hostile pages with the older build.
- The Scrapling `<0.3` pin limits the options (see TD-41a on the private-API
  coupling).

**Fix:** plan a bump, or route stealth through upstream Playwright.

**Done when:** browser smoke passes, and a qualification run on the Eyebox
retailer set shows the same or better yield and timing.

### TD-46 — Subrequest DNS failures and IPv6 gaps in the browser guard

**Priority:** P2 · **Area:** engine URL guard · **Risk:** medium · **Contract:** fewer failed pages behind a proxy · **Depends on:** one security review together with TD-23

- **a. Behind a proxy, one unresolvable subrequest host fails the whole page.**
  - A proxy is required in production (TD-18), and it makes
    `_requires_verified_dns` force strict resolution.
  - In `_guarded_async_intercept_route`, a subrequest host that doesn't resolve
    raises `URLResolutionError`. `BrowserSession.fetch.guard` turns that into a
    failed page and relaunches the browser on every attempt.
  - Exposure: a dead first-party subdomain on any target, or any dead
    third-party host on the 127 of 311 Eyebox browser targets that don't set
    `block_third_party`.
  - **Fix:** for subrequests, abort and record just that request. Keep failing
    and retrying for the main document. The request is still never let through
    unvalidated.
- **b. The IPv6 deny list has gaps.**
  - `engine/ip_policy.py` allows `::7f00:1` (IPv4-compatible),
    `::ffff:0:7f00:1` (IPv4-translated) and `fec0::1` (site-local).
  - These aren't routable in a normal Docker setup, so this is defense in
    depth.
  - **Fix:** deny `::/96`, `::ffff:0:0:0/96` and `fec0::/10`, or allow only
    `2000::/3`. The host firewall rules under `security/` change too.

### TD-47 — Eyebox cannot cancel its own runs: cancel requires the `delete` scope

**Priority:** P2 · **Area:** API auth · **Risk:** low · **Contract:** yes; needs a decision · **Depends on:** an operator decision

**Problem:**
- `cancel_job` authorizes `AuthScope.delete`.
- Eyebox's credential (`eyebox-local`, and production prescribes the same) has
  only `submit` and `read`. Its exact cancel in `SourceRun.reconcile`
  (`POST /jobs/{id}/cancel?run_id=`) therefore always gets a 403.
- Eyebox swallows that as `reconciled=False`,
  `reconciliation_error=HTTPStatusError`, and abandoned runs keep running until
  their budget runs out.
- Cancel only works with a legacy `SCRAPEYARD_API_KEYS` key, which has every
  scope.

**Options:**
1. Let `submit` cancel an exact `run_id` in its own projects. This is additive:
   a caller allowed to start a run should be able to stop it.
2. Add a narrow `cancel` scope and grant it to Eyebox. This also needs updating
   Eyebox's `KNOWN_SCOPES`.
3. Grant Eyebox `delete`, which also allows `DELETE /jobs/{id}`.

Option 1 is recommended.

**Done when:**
- A `submit`+`read` credential cancels its own project's exact run and gets
  204.
- The same credential still gets 403 for other projects and for
  `DELETE /jobs/{id}`.

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

Fold those into the same verdict cache. Review TD-46 under the same security
review.

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
