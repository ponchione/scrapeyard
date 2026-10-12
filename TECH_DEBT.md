# Technical debt and PR roadmap

This is Scrapeyard's working backlog. Unless an entry says otherwise, **each
open entry is one pull request**, and its lettered sub-items are commits within
it. Read [How to use this list](#how-to-use-this-list) and the
[Eyebox contract](#eyebox-contract) before starting any entry.

**Audit history:**
- **2026-10-12** (`447da7a`; nothing in this repository changed): the
  production host was connected to Eyebox's staging ingest and tested. It
  still runs release `523d805`, and this entry was checked against the host
  that evening.
  - Ingress is a Cloudflare Tunnel that the host opens outwards, not the
    reverse proxy and address allow-list TD-19 planned. An Access service
    token is checked at the edge and again by the connector, which forwards
    only the routes Eyebox uses. Tested from outside and from the staging
    ingest's container.
  - A reset with no shutdown (TD-20) came back by itself in 31 seconds with
    the databases and Redis intact. It lost two submissions acknowledged in
    its last three seconds and left the queue ahead of the database: TD-85,
    which the owner accepted for the beta.
  - The settings file gained a daily limit of 400 pages for each host and a
    24-hour cooldown after a denial. Until then the host ran with the
    Compose defaults: no page limit, and six hours.
  - One product page was requested for each of the four beta retailers
    (TD-18). Two refused the host's address at the first request and two
    served the page. The owner has not decided what to do about the two.
  - No Eyebox run has used the host yet. TD-17 lists the limits the two
    sides hold today; they do not agree.
- **2026-10-11** (`b371a37`): first deployment on the production host, a
  rented four-core virtual machine (Ubuntu 24.04.4, kernel 6.8.0-146, Docker
  28.5.2 and containerd.io 1.7.29 from Docker's repository, Compose 2.38.2).
  The release that had passed every lane did not start there, for three
  reasons, each found and fixed on the host before a new release was built:
  - TD-79: the egress probe's health check took 5.2 s of its three-second
    deadline. The host's cores are about half as fast as the runner's, the
    check runs under a quarter of a CPU, and its image has no bytecode, so
    each check compiled `asyncio` (3.3 s there), `argparse`, `secrets` and
    `socket.py`. It now imports built-in modules only and takes 0.2 s. The
    rehearsal times the check from now on.
  - TD-80: the host's runc is 1.3.3, which calls `openat2` after loading the
    seccomp filter. The profile refused it and the application's container
    died before it was up. Allowing `openat2` alone then broke thread
    creation: runc answers `ENOSYS` only above the highest system call a
    profile names, so `clone3` began to answer `EPERM`. The nine calls
    between are now named with `ENOSYS`. Neither the runner's runc nor 1.5.2
    needs `openat2` there, so no lane shows the effect (TD-82).
  - TD-81: Docker restarted that container without end and could not stop
    it ("did not receive an exit event"), so the wrapper's "the application
    is left stopped" was untrue, and the next deployment failed at its own
    first stop. The wrapper now removes a container that is not `exited`
    after the stop. Its first version, tried on the host, failed once
    because the removal can fail the same way; it is retried.
  An independent review found the probe and seccomp fixes sound and three
  defects in the first version of TD-81's: a container that could not be
  removed was left without its restart policy, a signal cut the clearing of a
  failed start short, and the removed container's last output went to the
  journal. It also found that the new seccomp test passed two broken
  profiles and that nothing tested what the health check verifies. All were
  closed before the merge and checked again by the same reviewer.
  With the three fixes applied by hand the application started, attested the
  policy, refused the host's own addresses, private ranges and the metadata
  address from its container, and completed `basic`, `dynamic` and `stealthy`
  fetches of a public page. TD-82 to TD-84 record what the lanes still do not
  cover. The release rebuilt from `523d805` was then installed and started
  by the boot service there. After a real reboot the unit had the
  application healthy 34 seconds after boot, with the policy, the profile and
  the attestation in its log, and the same checks passed again. A copy of the
  release with one changed file, and the release itself made writable by
  others, were both refused by `promote` with the running container
  untouched. TD-20 and TD-22 stay open for what is listed under them.
- **2026-10-10** (`2f6b70f` to the merge of pull request #12): deployment
  pass, begun because the owner is provisioning the production host.
  - A rehearsal lane now builds retained releases on a GitHub runner and
    deploys, refuses and rolls them back through the production wrapper, in
    untrusted mode, which no lane had run before (TD-22).
  - A boot service and a promotion command were added and rehearsed there
    (TD-20). An independent review of their first version found twelve
    problems, all fixed before the merge. A second review, of the merged
    code, found that a boot start which had waited for a promotion deployed
    the release the link named before the wait, and several lesser gaps;
    pull request #12 fixes them, and the same reviewer checked the fixes.
    Its own rehearsal run then failed on TD-77: `release_artifact.py create`
    named its image after a random temporary directory, and about one name
    in twenty is one Docker refuses. Fixed in the same pull request.
    Four of the first review's findings were in code already on `main`:
    TD-72 (the egress installer refused to run after the deployment network
    was recreated), TD-74 (its `remove` emptied a chain another interface
    still used), TD-75 (a failed start left the application container
    restarting) and TD-76 (nothing kept two deployments, or two policy
    installations, from interleaving).
  - TD-71 was diagnosed and fixed: no container restriction hung the crashed
    renderer; the kernel was holding it while its core was dumped.
  - TD-73 (Chromium ran without its sandbox on fetches without `stealth`) was
    found on the way and fixed in pull request #10.
  - TD-22 and TD-20 stay open for their checks on the production host.
  - The owner then left the open decisions to the maintainer's judgement and
    ruled out an operator proxy on cost. Pull requests #10 (TD-73) and #3
    (TD-65) were merged, after a sandboxed Chromium was started inside
    Eyebox's local container and every Eyebox config and target hostname was
    checked against the new hostname rule. Production will run first-party
    submissions with no proxy (TD-18). The review of that change found TD-78:
    the host egress policy did not cover the host's own addresses. A probe in
    the smoke lane showed the application's container reaching a listener on
    the bridge gateway with the policy installed (run 38094674579); the
    installer now refuses that input, and the smoke lane and the rehearsal
    probe it.
- **2026-10-10** (`12a23e6` to `166ceac`): first Actions pass. The owner
  re-enabled Actions and `main` was pushed for the first time since
  2026-09-25. On the first runs Dependency audit, Container security, the
  distribution build, live Redis and both qualification profiles passed; the
  Quality job and browser smoke failed. Fixing them closed TD-26 and found
  three defects the local checks could not: TD-68 (the egress policy filtered
  nothing on the deployment bridge under Docker 27.4 or newer), TD-70 (a
  crashed page could hold a browser fetch open until the run deadline) and
  TD-69 (CI skipped the real-browser tests and floated on `ubuntu-latest`).
  All three are fixed. TD-71 was recorded. TD-26c (sharing image layers
  between jobs) was dropped: cold builds took 101 to 188 s in jobs of 4 to
  10 minutes, and each lane rebuilds on purpose. The lone-surrogate half of
  TD-65 landed; the rest is pull request #3, awaiting a security review.
- **2026-10-09** (`8f564a6` to `bc3041e`): third roadmap pass, on the owner's
  decisions for the blocked entries. TD-62 was closed (Node 24 action pins;
  nothing has run them yet, see TD-26b), as were the coordinated TD-23 (a run
  resolves each hostname once per 30 seconds) and TD-60 (the frame-detach race
  was accepted and documented, and the smoke check fixed). An independent
  adversarial review of TD-23 found two defects in its first version, fixed
  before the merge, and two older ones: TD-66, fixed, and TD-65, left for a
  security review. TD-67, a unit test that needed 15 GiB of free temp disk,
  was found by the final run and fixed.
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
5. **Run the checks locally, then open a pull request.** GitHub Actions runs
   them again, on Python 3.10 and 3.12, and runs the container lanes when
   their paths change. Read a failed job's log with
   `gh api repos/{owner}/{repo}/actions/jobs/<job_id>/logs`.

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
7. **New findings** take the next free ID (**TD-86**) and use this template:

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
| After the third 2026-10-09 pass (`bc3041e`) | ruff, mypy (107 files) and `pytest -W error` pass on 3.12 and 3.10: 2,604 passed, 17 skipped, 90.44% coverage; the 17 live Redis tests pass against a throwaway Redis; Eyebox's client tests (297 passed) and 54 configs pass; the two Chromium modes of the fetch-session smoke check pass locally (Camoufox is not installed here). The lock and the image did not change, so the dependency audits and the container scan were not rerun |
| After the first Actions pass (2026-10-10, `166ceac`) | Locally, ruff, mypy (107 files) and `pytest -W error` pass on 3.12 and 3.10: 2,614 passed, 17 skipped, 90.44% coverage. On GitHub at the same commit: CI (Quality 3.10: 2,611 passed, 20 skipped; Quality 3.12: 2,614 passed, 17 skipped, with Chromium installed for the three real-browser tests; distribution build; live Redis), Dependency audit on both legs, Container security, browser smoke (4 min; image 3.36 GB), and release qualification quick (10 min) and full (23 min) all pass, with no deprecation or runner warnings. Eyebox's client tests (299 passed) and 54 configs pass |
| After the deployment pass (2026-10-10, `2c0f4be`; 2,695 passed after pull request #12) | Locally, ruff, mypy (107 files) and `pytest -W error` pass on 3.12 and 3.10: 2,676 passed, 17 skipped, 90% coverage. On GitHub, CI, Dependency audit, Container security, browser smoke and quick qualification pass on the commits that touch them, and the release rehearsal passes on the pull request head `cf335c8` (run 38073376177: two releases built, scanned and qualified in 11 and 10 minutes, the rehearsal with the boot service in 406 seconds, 29 minutes in all). Run 38071059498 on `main` at `f100488` uploaded the first release archive (1.3 GB) |

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

Ready as single pull requests, in this order: TD-82, then TD-84.

CI gates pull requests since 2026-10-10. A release (TD-22) is built by the
`Release artifact and deployment rehearsal` workflow, which scans, qualifies
and rehearses it; dispatch it on the commit to release.

**Coordinated:**
- TD-83: needs a security review.
- TD-45: needs the owner's choice of route, and Eyebox to move with it.
- TD-24: needs real `reuse_browser` runs.
- TD-17 to TD-22: deployment qualification; not single PRs. The host is up
  and connected (2026-10-12), so what is left is this:
  - TD-18 and TD-17 wait for the owner: what to do about the two beta
    retailers that refuse the host's address, and leave to run each beta job
    from it. Every retailer request from the host needs the owner's word.
  - TD-19 needs its runbook written and one run from Eyebox's ingest.
  - TD-20 needs a monitoring service and TD-21 an off-host store, both the
    owner's choice.
  - TD-22 needs a second release on the host to roll back from.

**Deferred:** TD-48, TD-49 and TD-85.

**Consumer-side:** C-1 to C-10 are fixed in Eyebox.

## Index

| ID | P | Status | Finding |
| --- | --- | --- | --- |
| TD-17 | P2 | operational | Eyebox and Scrapeyard limits lack a jointly qualified operating envelope |
| TD-18 | P1 | operational | Production forward-proxy selection and egress qualification are incomplete |
| TD-19 | P1 | operational | The production ingress is in no runbook and no Eyebox run has used it |
| TD-20 | P1 | operational | Secure startup after host/Docker restart and runtime monitoring are unqualified |
| TD-21 | P1 | operational | Unattended coordinated off-host backup and restoration are incomplete |
| TD-22 | P2 | operational | Deployment and rollback lack retained, qualified release artifacts |
| TD-24 | P3 | coordinated | Browser reuse and its script cache are tuned on fixtures, not real traffic |
| TD-45 | P1 | coordinated | Bundled browsers are months behind upstream |
| TD-48 | P3 | deferred | Stored configs are duplicated and uncompressed |
| TD-49 | P3 | deferred | Config and engine features Eyebox doesn't use |
| TD-82 | P2 | ready | The rehearsal runner's container runtime is older than a patched host's |
| TD-83 | P2 | coordinated | The seccomp allowlist predates the system calls added since Linux 5.1 |
| TD-84 | P2 | ready | No lane has run a release on Docker 29 |
| TD-85 | P2 | deferred | A power loss loses the submissions accepted in its last seconds |

## Ready to implement

### TD-82 — The rehearsal runner's container runtime is older than a patched host's

**Priority:** P2 · **Area:** CI, deployment · **Risk:** low · **Contract:** none · **Depends on:** —

**Problem:** The rehearsal and smoke lanes use whatever Docker, runc and
Compose GitHub's `ubuntu-24.04` image carries (Docker 28.0.4 and Compose
2.38.2 on 2026-10-10). The first production host, installed from Docker's
repository a day later, got runc 1.3.3, and a release that had passed every
lane could not start a container there (TD-80, fixed). The lanes still run
the older runtime, so they would pass that release today, and they will meet
the next such difference only when the runner image happens to change.
**Code:** `.github/workflows/release-rehearsal.yml`, `.github/workflows/ci.yml`
(the smoke job), `docs/DEPLOYMENT.md`.
**Fix:** Before a lane builds anything, install pinned `docker-ce`,
`containerd.io` and the Compose plugin from Docker's repository: the versions
the deployment guide names for a host. Write `docker version` and
`runc --version` into the lane's evidence, and state the versions in the
guide. Raise the pins and the guide together.
**Done when:** A lane's evidence shows the pinned versions, and they are the
ones the production host runs. With runc 1.3.3 among them, a rehearsal of
`b371a37` fails at its first deployment with "unsafe procfs detected"; runc
1.5.2 does not show it.

### TD-84 — No lane has run a release on Docker 29

**Priority:** P2 · **Area:** deployment · **Risk:** medium · **Contract:** none · **Depends on:** TD-82

**Problem:** The production host runs Docker 28.5.2 and Compose 2.38.2, held
at those versions, because no lane has run anything newer. Docker's
repository offered 29.9.0 and Compose 5.6.0 on 2026-10-11. One difference to
check first: `release_artifact.py load` compares the IDs of the images it
loads with the release's manifest, and Docker 29 is reported to store images
with containerd on a new installation, which changes the ID an image is
given. Nothing here has been run on it. On 2026-10-12 the four held packages
(`docker-ce`, `docker-ce-cli`, `containerd.io` and the Compose plugin) were
the host's only pending upgrades: until this entry is done the host takes no
fixes for them, security fixes included.
**Code:** `scripts/release_artifact.py` (`load`, `verify`),
`security/deploy-secure-compose.sh`, `scripts/run_deployment_rehearsal.sh`.
**Fix:** With TD-82's pins, run the rehearsal on Docker 29 and Compose 5. Fix
what fails, then raise the pins, the guide and the host.
**Done when:** A dispatched rehearsal passes on Docker 29 and the production
host has been upgraded and rebooted on it.

## Coordinated

These need a decision, a security review, an Eyebox change, or real-traffic
data before any code.

### TD-83 — The seccomp allowlist predates the system calls added since Linux 5.1

**Priority:** P2 · **Area:** security, browsers · **Risk:** medium · **Contract:** none · **Depends on:** —

**Problem:** `security/seccomp/chromium.json` is the profile Playwright
publishes: Docker's default list of about 2020 plus `clone`, `setns` and
`unshare` for Chromium's user-namespace sandbox. Its highest-numbered system
call was `io_uring_register` (427) until TD-80 added `openat2`. Everything
newer (`faccessat2`, `close_range`, `clone3`, `epoll_pwait2`, `landlock_*`,
`process_madvise` and the rest) answers `ENOSYS`, and callers fall back. That
works today: browser fetches passed on the production host on 2026-10-11. It
holds only while every caller treats `ENOSYS` as an old kernel, and runc
1.3.3 was the first that did not. The next one fails the same way, in
production first (TD-82).
**Code:** `security/seccomp/chromium.json`,
`tests/unit/test_browser_runtime_contract.py`.
**Fix:** Derive the profile again from the current Moby default plus the
three user-namespace calls. Decide `clone3` on purpose: Moby answers `ENOSYS`
because a filter cannot read its flags, and this profile already allows
`clone` unfiltered. Keep the difference from the Moby default as a short list
beside the profile, and have the change reviewed as a security change.
**Done when:** The reviewed difference is recorded, and the smoke lane, the
rehearsal and a deployment on a host with the current runc pass.

### TD-45 — Bundled browsers are months behind upstream

**Priority:** P1 · **Area:** dependencies, browsers · **Risk:** medium · **Contract:** Eyebox's hard-test venv (`../eyebox/.hard-test/scrapeyard-venv`) and the host's `~/.cache/ms-playwright` must move in lockstep · **Depends on:** coordination with Eyebox

**Problem:**
- rebrowser-playwright 1.52 ships Chromium 136.0.7103, which the stealth
  path of the dynamic fetcher uses. Playwright 1.58 ships Chromium 145.
- There is nothing to bump to: on 2026-10-10 PyPI's newest
  rebrowser-playwright was still 1.52.0 (2025-05-09). The newest of the others
  were playwright 1.63.0, patchright 1.63.0, camoufox 0.5.8 and scrapling
  0.4.15. Moving the stealth path means choosing another driver.
- Scrapeyard loads hostile pages with the older build.
- The Scrapling `<0.3` pin limits the options: `browser_session.py` calls
  name-mangled Scrapling internals, which is fine only while the version stays
  below 0.3 (0.2.99 is the last 0.2 release).

**Spike (2026-10-10):** two routes were built on branches and run through the
browser smoke lane on a GitHub runner. Both passed every fetcher mode and the
fetch-session checks at the first attempt. Neither is merged.

| | Today | Upstream Playwright (`td-45-spike-upstream`) | patchright 1.63.0 (`td-45-spike-patchright`) |
| --- | --- | --- | --- |
| Stealth Chromium | 136.0.7103 (rev 1169) | 145.0.7632 (rev 1208) | 153.0.8010 (rev 1243) |
| Image size | 3.36 GB | 2.75 GB | 3.31 GB |
| Smoke run | 38053605687 | 38053992070 | 38053993427 |
| Eyebox local stack | — | nothing to install | needs `patchright==1.63.0` in its venv and `chromium-1243` in the host cache, or the service will not import |

Measured on a development host against loopback fixtures, on the stealth path:
- `navigator.webdriver` is false on all three, and the classic
  `Runtime.enable` probe (an Error `stack` getter) is silent on all three. It
  fires only on Chromium 136 with an unpatched driver.
- A variant of that probe (an Error `name` getter) fires once today, twice on
  upstream and once on patchright. Whether real detectors use it is unknown.
- Page-visible `__pw*` globals: 3 today, 4 on upstream, 1 on patchright. One
  of them, `__pwWebSocketDispatch`, comes from Scrapeyard's own WebSocket
  guard on every route.
- Upstream captures page console messages for the classifier; rebrowser and
  patchright capture none.

Also found:
- Scrapling 0.2.99 requires rebrowser-playwright, so the package (about
  130 MB) stays installed on either route; only its Chromium can go.
- Scrapling itself moved to patchright (0.3.14 pins 1.56.0; 0.4.15 needs
  1.62.1 or newer).
- patchright 1.58.2 ships the same Chromium revision 1208 as the pinned
  playwright (from its wheel manifest, not built): a middle route with one
  browser build and nothing new for Eyebox to download.
- camoufox 0.5.8 requires `playwright<1.63`.

Not measured: yield or timing against any retailer or anti-bot vendor (148
of the 337 targets in Eyebox's configs use dynamic stealth), Python 3.10, and
whether the new Chromium starts inside Eyebox's Playwright 1.58.2 container.

**Fix:** the owner chooses the route. The spike branches hold the code change
for either (ten files each); README and `docs/` still need the rename.

**Done when:** browser smoke passes, and a qualification run on the Eyebox
retailer set shows the same or better yield and timing.

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

**Set but not qualified (2026-10-12):** the production host and Eyebox's
staging ingest, which points at it, hold the values below. They were read
from the running containers. No job has run between the two.

| Limit | Production host | Staging ingest | Gap |
| --- | --- | --- | --- |
| Run duration / client wait | 2,700 s | waits 1,200 s | Ingest stops waiting 25 minutes before the server's budget ends. Runs took 20 to 25 minutes on a development machine whose cores are about 3.4 times as fast as the host's; nothing has been timed on the host. |
| Result size | 16 MiB | 10 MiB | The hard-test pairing: a result between the two is rejected by Eyebox. |
| Record count | 5,000 | 5,000 | None. |
| Memory admission | 3,072 MiB (Compose default) | — | Never exercised on the host. |
| Concurrency | 2 workers | 4 scheduled jobs at once | Two of four submissions wait in the queue, inside ingest's 1,200 s. |
| Host guards | 400 pages a day for each host; 24-hour cooldown after a denial | — | One refused request stops every run against that retailer for a day. |

Two things send traffic outside the schedule or interrupt it, and belong in
the envelope:
- With an API key configured, ingest's alert price check fetches a retailer
  page through Scrapeyard before it sends an alert, whether or not scheduled
  scraping is enabled.
- The host installs security updates unattended and reboots at 10:30 UTC when
  one needs it. Whether the beta schedules avoid that time was not checked.

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

**Priority/ownership:** P1; Scrapeyard deployment/security. Supplies TD-17's
egress path.

**Decision (owner, 2026-10-10):** no operator proxy, on cost. Eyebox is the
only submitter and sends configurations from its own repository, so production
sets `SCRAPEYARD_UNTRUSTED_SUBMISSIONS=false` and fetches directly from the
host's address ([DEPLOYMENT.md](docs/DEPLOYMENT.md#without-an-operator-proxy)).
- The host egress policy and the application's startup check of it stay
  mandatory; the deploy wrapper refuses to run with that check switched off.
- A retailer that blocks the host's address is accepted as lost, or given a
  proxy by itself with `proxy:` on its targets, which this mode allows
  without `transport-admin`.
- The deploy wrapper accepts the mode, and the deployment rehearsal's last
  steps promote, start and attest a release with these settings.

**Done on the production host:**
- 2026-10-11: from the application's container, connections to the host's own
  addresses, private ranges and the metadata address were refused, and
  `basic`, `dynamic` and `stealthy` fetches of a public page completed
  (recorded under TD-20).
- 2026-10-12: one product page was requested, once, for each of the four beta
  retailers, with the browser settings Eyebox's price check uses. Two refused
  the first request (an HTTP 429 challenge page and an HTTP 403 denial). Two
  served the page, and the price and stock were extracted. All four had
  served the same settings from a home connection in September, so the
  refusals are about the address.

**Still open:**
- The owner's decision for the two that refuse: a proxy on those targets
  alone (`proxy:` in Eyebox's configs, which needs no change here), another
  address, or leaving them out. Eyebox's greenlight G3.2 already plans a
  pay-as-you-go residential proxy for retailers blocked from the host; the
  decision above ruled out only a service-wide one. Whether Eyebox's beta
  goal (150 products at two or more retailers, 40 at three or more) holds
  without the two has not been computed.
- One whole job for each beta retailer from the host (Eyebox's greenlight
  G3.1). One product page says nothing about category pages, pagination or a
  run's volume from this address.
- The rest of the denial matrix below through the application on the host:
  redirects and DNS rebinding. The lanes cover both against fixtures.
- The per-retailer results are in the host's stored results and the
  operator's notes only. They belong in Eyebox's greenlight document (C-10).

The proxy selection work below is moot unless a service-wide proxy is adopted
later.

**Problem (as recorded before the decision):** production Compose enables untrusted submissions, and startup
requires a nonempty, non-`direct` `SCRAPEYARD_PROXY_URL`. The stack supplies
Redis and an egress probe but no filtering forward proxy. The HTTPS ingress
proxy in TD-19 does not satisfy this requirement. A commercial scraping proxy
is not automatically evidence of safe destination filtering.

**Checked 2026-10-10:**
- The operator proxy must be at a public address, and its name must resolve,
  when the service starts (`normalize_service_proxy_url`). A proxy on a
  private or loopback address, or on the deployment bridge, is refused at
  startup, so a forward proxy hosted beside Scrapeyard has to be reached at a
  public address.
- `SCRAPEYARD_EGRESS_ALLOW_CIDRS` changes the host policy only; the
  application never reads it. The example in `docs/DEPLOYMENT.md`
  (`scrape-proxy.example` with an allowed documentation address) therefore
  cannot start as written.
- In untrusted mode every target inherits that one proxy. A target bypasses
  it (`proxy: direct`) or names another only when the submitting credential
  has `transport-admin`. Sending some targets through a paid proxy and the
  rest directly therefore needs that scope on the submitting credential, or
  trusted mode. Either gives up part of what untrusted mode is for, so it is
  the owner's decision.
- No lane runs a successful scrape through an operator proxy. The deployment
  rehearsal runs untrusted mode with a proxy address that is made unreachable.

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

### TD-19 — The production ingress is in no runbook and no Eyebox run has used it

**Priority/ownership:** P1; Scrapeyard ingress/security. Until 2026-10-12
this entry was "Restricted Hetzner HTTPS ingress and Railway credential
handoff are incomplete".

**Done on 2026-10-12, by another route than the one planned below:** the
owner's domain is on Cloudflare, so ingress is a Cloudflare Tunnel that the
host opens outwards, with an Access service token. It replaces the reverse
proxy, the certificate and the allow-list of Railway's addresses, and costs
nothing.
- The host's firewall still admits SSH only. From outside, its address
  answers on no other port.
- Access answers 401 to a request without the token, or with a wrong one.
  The connector checks the token again and forwards seven routes: `/health`,
  `POST /scrape`, `GET /scrape/acceptance`, `GET /jobs/{id}`,
  `GET /jobs/{id}/runs/{run}`, `POST /jobs/{id}/cancel` and
  `GET /results/{id}`. Thirty other paths and spellings (dot segments,
  encoded and doubled slashes, upper case) got 404 from the connector or 400
  from the edge. Readiness, metrics, the job list and the domain-guard routes
  cannot be reached from outside.
- The path filter cannot tell methods apart, so `PUT` and `DELETE /jobs/{id}`
  reach the application. It refuses them: Eyebox's credential holds `submit`
  and `read` for project `eyebox` only.
- The connector's unit may reach the application's address, the resolver and
  the public internet. Redis, the probe, loopback, the host's own addresses
  and private ranges were tried from it and dropped.
- Through the public name, a scrape of a public page was submitted, found
  again through `/scrape/acceptance`, polled and read back.
- Eyebox's ingest sends the token since its `03837e4`, and only to the
  Scrapeyard origin. From the staging ingest's container on Railway,
  `/health` answered 200 and an authenticated read reached the application.
- The connector reconnected by itself after the reset under TD-20.

**Still open:**
- No scrape has been submitted from Railway. The first whole run from the
  staging ingest (TD-17) proves submit, poll and download.
- None of it is in a runbook. [DEPLOYMENT.md](docs/DEPLOYMENT.md) does not
  mention a tunnel. The connector's unit and its address restrictions, the
  route list, and the host's firewall, SSH and update settings exist on the
  host only, so the host cannot be rebuilt from what is written down.
  Eyebox's runbook and topology still describe a reverse proxy on another
  provider's server (C-10).
- Rotation is neither documented nor tried, for the service token (it expires
  on 2027-10-12) or for the API credentials.
- Not tried through the tunnel: a request over the size limit, and whether
  the edge's or the connector's logs hold a credential.
- The zone's Browser Integrity Check answers 403 to some client signatures
  (Python's `urllib` is one) before Access sees the token. Eyebox's `httpx`
  passes. Check it again when the client library changes.

**Problem (as recorded before 2026-10-12; the reverse-proxy and address-list
work below is moot unless the tunnel is given up):**
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

**Implementation update (2026-10-10):**
- `security/systemd/scrapeyard.service` and `security/manage-release.sh`
  (installed as `scrapeyard-release`) are checked in;
  [DEPLOYMENT.md](docs/DEPLOYMENT.md#boot-service-and-promotion) has the
  layout and the commands.
- Rehearsed on a GitHub runner (Ubuntu 24.04, systemd 255, Docker 28.0.4; run
  38073376177), each step ending with the images, confinement, policy, single
  instance and a known run verified:
  - installation as the guide describes it;
  - a promotion refused for a changed file and for a file root does not own,
    and one stopped halfway, each leaving the link and the running release;
  - a Docker restart, and a killed Docker daemon (back in 3 seconds);
  - Docker stopped, the policy removed, `br_netfilter` unloaded and the
    AppArmor profile unloaded, then Docker started: that start alone brought
    the unit and the application back;
  - the containers and the network removed: the policy followed the new
    bridge and the known run was still readable;
  - a broken setting: three retries with nothing serving, then recovery
    without an operator command once the file was repaired;
  - a promoted rollback to a release from before the boot service existed,
    started through the unit;
  - since pull request #12: a release that root owns but that sits in a
    directory another account can write to, refused; a promotion stopped
    after it had stopped the application, ending stopped and saying so; and
    the unit started while a promotion was under way, starting the release
    that was promoted (run 38079616422 showed it wait 20 seconds for the lock
    and then load the promoted release).
- A failed start is retried with growing pauses and is never abandoned. A
  systemd start limit also counts successful starts, so "retries stay
  bounded" is met by slowing them down, not by giving up.

**Done on the production host (2026-10-11, release `523d805`):** a real
reboot. The unit stopped the application before Docker went down, Docker did
not start it at boot, and the unit had it healthy 34 seconds after boot with
the host rules, the egress policy, the AppArmor profile and the attestation in
its log. From the application's container the probe's challenge, the host's
own addresses, private ranges and the metadata address were refused, and
`basic`, `dynamic` and `stealthy` fetches of a public page completed. The
host's inbound firewall is a default `ufw`; the `INPUT` jump stayed ahead of
its chains.

**Done on the production host (2026-10-12):** a power loss, as a kernel reset
with no sync and no shutdown, so nothing had stopped the container. The host
was back and the application healthy 31 seconds later with no operator
command, and the application does not report healthy before its startup
check of the egress policy passes. The filesystem, the three SQLite files
(`integrity_check`) and Redis's append-only file were intact, and a run
finished 40 seconds before the reset was still readable. What the reset lost
is TD-85.

**Still open:**
- All of the monitoring work below: readiness and metrics collection, and
  alerts. It needs the owner's choice of a monitoring service. Nothing
  collects or alerts today: a stopped application, a full disk or a dead
  tunnel would show as a failed Eyebox run or not at all. A credential for
  the detailed health route exists on the host and nothing uses it.
- The health check Compose runs is still process liveness only.
- Nothing watches the host policy between deployments. The startup check
  covers forwarded traffic; the `INPUT` jump that keeps the application off
  the host's own addresses (added for TD-78) can be removed by another
  firewall tool without the application noticing. An alert on a missing jump
  belongs with the monitoring work.

**Problem:**
- Before 2026-10-10, `security/deploy-secure-compose.sh` installed policy
  during a deploy, and no checked-in boot service restored it after a host or
  Docker restart.
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
- Run the egress installer at every boot. Since TD-68 it also loads
  `br_netfilter` and sets `net.bridge.bridge-nf-call-iptables`, which the
  policy needs and which does not survive a reboot.
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

**State on the production host (2026-10-12):** no backup of any kind runs
there, and no off-host store has been chosen. The two data volumes hold
4.5 MB. The encryption key was generated on the host and is nowhere else, so
a lost host loses the stored configurations with it, and Eyebox needs a new
credential for the rebuilt one. Rebuilding also depends on the runbook TD-19
still owes.

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

**Implementation update (2026-10-10):**
- The `Release artifact and deployment rehearsal` workflow ran
  `release_artifact.py create` to completion for the first time, on a GitHub
  runner: 10 to 12 minutes for each release, scan and quick qualification
  included.
- `scripts/run_deployment_rehearsal.sh` then deploys the candidate with the
  production Compose file, in untrusted mode, through the wrapper the release
  retains. Neither container lane does that: both overlays turn untrusted
  mode off. It checks the images, the confinement, the host policy, one
  instance and the API after each step:
  - a release with a changed file is refused with the running application
    untouched;
  - a policy that cannot be installed leaves the application stopped;
  - started without the policy, or with it on another bridge, the application
    reports the reachable challenge and never serves;
  - a rollback and a roll-forward keep a known run readable, and its
    submission is replayed, not run again;
  - no image is pulled.
- A manual dispatch uploads the rehearsed release. Run 38071059498 uploaded
  `scrapeyard-release-f100488…` (1.3 GB, kept 30 days). Since `2c0f4be` the
  archive records root as the owner of every file and the workflow checks
  what extracting it gives.
- [TESTING.md](docs/TESTING.md#release-and-deployment-rehearsal) describes
  the lane and [DEPLOYMENT.md](docs/DEPLOYMENT.md#retained-release-artifacts-and-rollback)
  the commands to fetch a release.

**Done on the production host (2026-10-11):** release `523d805` was fetched
as the workflow's artifact, checked against the run's checksum, extracted as
root and started with nothing built or pulled; it serves with no operator
proxy, the owner's choice (TD-18). `promote` refused a copy with one changed
file and the release itself while others could write to it, and left the
running container as it was.

**Still open:** a rollback on the production host. The only earlier release,
`b371a37`, cannot start there (TD-79 to TD-81), so the first rollback will be
from the next release to `523d805`. The same checks behind the real ingress
(TD-19). Keep a previous release on the host and off it; the workflow
artifact expires after 30 days. On 2026-10-12 the host held `523d805` alone,
and the artifacts of `523d805` and `b371a37` expire on 2026-11-10. After that
the copy on the host is the only one until the workflow is dispatched again.

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

### TD-85 — A power loss loses the submissions accepted in its last seconds

**Priority:** P2 · **Status:** deferred · **Contract:** `POST /scrape` answers 202 for a submission that a power loss can still lose; afterwards `GET /scrape/acceptance` and the run's poll answer 404

**Problem:** found on the production host on 2026-10-12 by the reset under
TD-20.
- SQLite runs in WAL with `synchronous=NORMAL`
  (`src/scrapeyard/storage/database.py`), so a commit reaches the disk when
  the kernel writes it back, up to 30 seconds later on that host. Two
  submissions answered 202 in the three seconds before the reset had no job
  row afterwards. [JOB_LIFECYCLE.md](docs/JOB_LIFECYCLE.md) names the limit;
  this is the first time it was measured.
- Redis syncs every second, so it kept the queue entry and the in-progress
  marker of the submission that was running. After the reset the queue held
  a task for a job the database did not have. When the marker expired, one
  run budget (45 minutes) later, the queue delivered the task again. It was
  recorded as finished and the application logged nothing. Whether it
  fetched the page was not established.

**Why deferred:** the owner accepted it for the beta on 2026-10-12: no code
change and no release for it. Not tried: what Eyebox does when a run it was
given a 202 for answers 404.

**Revisit when:** a lost submission is seen in practice, or a second consumer
relies on a 202. Start with the cheap half: establish what the worker does
with a task whose job row is missing (`scrape_task`,
`_load_job_execution_context` in `src/scrapeyard/queue/worker.py`). It should
end without a fetch and say so in the log. `synchronous=FULL` adds a disk
sync to every commit and has not been measured on the host.

## Consumer-side follow-ups (fix in Eyebox)

These were found while mapping the contract. They are fixed in `../eyebox`, not
here; record them in Eyebox's tracker when picked up.

| ID | Finding | Where (Eyebox) |
| --- | --- | --- |
| C-1 | `POST /scrape` is sent with `retry=False`. A busy 503 (`Retry-After: 5`) falls through to `/scrape/acceptance`, gets a 404, and the run fails. Price check marks the result `UNAVAILABLE`. Retrying is safe, because the same key and bytes replay. | `ingest/scrapeyard_run.py` (submit), `pipeline/price_check.py`, `pipeline/offer_accuracy.py` |
| C-2 | The alias backfill's `DELETE /domains/{host}/guard` needs `transport-admin` and an unscoped caller, so it gets a 403 that is never checked. | `ingest/pdp_alias_backfill.py` |
| C-3 | `SCRAPEYARD_HEALTH_PROBE_API_KEY` and `SCRAPEYARD_LOCAL_DEVELOPMENT_UNAUTHENTICATED` are set but Scrapeyard reads neither. The smoke tool parses `dependencies.*` from a 503 on `/health`, but `/health` is now public liveness (`{"status":"ok"}`) and the detail lives at authenticated `/health/ready`. | `scripts/hard-test.sh`, `scripts/hard-test-cold-start.sh`, `ingest/scrapejob_smoke.py` |
| C-4 | The wait-budget defaults disagree: the code and `.env.prod.example` use 300 s, while Compose defaults to 2,700 s. An operator who copies `.env.prod.example` overrides Compose. This feeds TD-17. On 2026-10-12 the staging ingest waited 1,200 s against the production host's 2,700 s, and ran four scheduled jobs at once against the host's two workers. | `ingest/config.py`, `.env.prod.example`, the staging service's variables |
| C-5 | The hard-test Scrapeyard container runs without an init process (`Init=<nil>` on 2026-10-09), even though `DEPLOY.md` requires one. Launch it with `--init`. Optionally, Scrapeyard could warn at startup when it runs as PID 1. | `.hard-test/scrapeyard-local/` launcher |
| C-6 | The docs disagree on webhooks. `DEPLOY.md` says Scrapeyard signs `X-Webhook-Signature`; it doesn't, as `README.md` correctly says. `AGENTS.md` and `README.md` say ingest "receives Scrapeyard webhooks", but production pulls. 51 configs still carry `webhook` blocks. `make scrape-live` without strip sends them, and Scrapeyard's dispatch-time SSRF check blocks `host.docker.internal`. | `DEPLOY.md`, `README.md`, `AGENTS.md`, `configs/` |
| C-7 | 11 configs list 403 in `retryable_status`, which Scrapeyard never retries. `adaptive_domain` on 11 Brownells targets does nothing with `adaptive: false`. Both are cosmetic; keep the keys valid. | `configs/` |
| C-8 | Target summary URLs have their query values redacted, so exact URL matching fails for 119 query-string targets across 8 configs, and diagnostics and the backfill fall back to position. This works today; don't add exact-URL logic without a Scrapeyard change. | `ingest/scrapejob_diagnostics.py`, `ingest/pdp_alias_backfill.py` |
| C-9 | The docs disagree on the pinned production Scrapeyard revision: `ce8fc04` vs `5de7dc9`+. `scrapeyard_identity.source_run()` (legacy `runs[]` shape) is used only by tests. | `docs/hetzner-scrapeyard-deployment-runbook.md`, `docs/scrapeyard-work-orders.md`, `ingest/scrapeyard_identity.py` |
| C-10 | The launch list (item 2), the runbook and the topology describe a reverse proxy on a Hetzner server. Since 2026-10-12 production is another provider's server behind a tunnel with a service token (TD-19), and the launch list shows the item open with none of that recorded. The results for the four beta retailers from the host's address (TD-18) are not in the greenlight document, whose Phase 3 is still unticked. | `GO-LIVE-TESTING-CHECKLIST.md`, `docs/hetzner-scrapeyard-deployment-runbook.md`, `docs/deployment-topology.md`, `docs/beta-retailer-greenlight.md` |
