# Dynamic-stealth runtime mismatch implementation plan

> For Hermes: use subagent-driven-development if this gets executed later. This document is planning only.

Goal: make Scrapeyard's Dockerized `fetcher: dynamic` path work both with `browser.stealth: false` and `browser.stealth: true`, so hostile-retailer debugging is not blocked by a broken local runtime.

Architecture: keep the existing Scrapling/Playwright fetch flow unchanged and fix the environment contract instead. The container already installs stock Playwright Chromium for the normal dynamic path; this slice should explicitly install the rebrowser Chromium revision required by Scrapling's dynamic-stealth path and document the invariant so rebuilds remain deterministic.

Tech stack: Docker, Docker Compose, Poetry, Scrapling, Playwright, rebrowser-playwright, FastAPI service image.

---

## Current grounded context

Observed repo/runtime facts:
- `Dockerfile:20-32` installs Python deps, then runs `python -m playwright install --with-deps chromium` and `python -m camoufox fetch`.
- The plan doc at `docs/plans/2026-04-20-hostile-retailer-capability-priorities.md:42-77` identifies the first priority as fixing dynamic-stealth runtime mismatch.
- The handoff at `handoffs/eyebox-agent-basspro-cabelas-scrapeyard-access-readout-2026-04-20.md:193-219` records the concrete mismatch:
  - stock Playwright Chromium revision present
  - `rebrowser-playwright` expects a different Chromium revision
  - fastest in-container repair was `docker compose exec scrapeyard rebrowser_playwright install chromium`
- `README.md:124-149` currently documents Docker usage as installing Playwright Chromium, but does not mention the additional rebrowser browser dependency for `browser.stealth: true` on the dynamic path.
- Existing unit coverage around browser kwargs/debug lives in `tests/unit/test_scraper_decomposition.py`; there does not appear to be current automated coverage for Docker browser-runtime assumptions.

Non-goals for this slice:
- do not try to make Bass Pro/Cabela's succeed
- do not add proxy/session orchestration
- do not widen browser feature surface yet
- do not refactor scraping logic unless required for deterministic runtime verification

---

## Proposed approach

1. Make the Docker image install both browser runtimes it advertises:
   - stock Playwright Chromium for normal `dynamic`
   - rebrowser Chromium for `dynamic` + `browser.stealth: true`
2. Keep the install step deterministic at image build time, not as a manual post-start command.
3. Add a cheap verification step and docs so the contract is obvious to future maintainers.
4. Validate both dynamic modes in-container with minimal probes after rebuild.

---

## Task breakdown

### Task 1: Confirm the exact rebrowser install command and expected artifact location

Objective: remove ambiguity before editing the image build.

Files:
- Read: `handoffs/eyebox-agent-basspro-cabelas-scrapeyard-access-readout-2026-04-20.md`
- Read: `Dockerfile`
- Read: `poetry.lock`

Steps:
1. Confirm the installed package name/version in `poetry.lock` (`rebrowser-playwright`).
2. Confirm the known-good command from the handoff (`rebrowser_playwright install chromium`).
3. If execution starts later, verify inside a fresh container whether the CLI exists directly or whether `python -m rebrowser_playwright install chromium` is the portable invocation.
4. Prefer the invocation that is explicit and stable in Debian slim images.

Acceptance:
- The implementation uses a command that works in the container without relying on shell-session trivia.

Open question:
- whether CLI entrypoint or `python -m ...` is the more robust build-time invocation. This should be resolved by live container verification during execution.

### Task 2: Update the Docker image build to install rebrowser Chromium deterministically

Objective: fix the broken runtime contract in the image itself.

Files:
- Modify: `Dockerfile:20-32`

Planned change:
- Keep the current apt/pip install flow.
- After `python -m playwright install --with-deps chromium`, add the rebrowser browser install step.
- Preserve `python -m camoufox fetch` for the `stealthy` path.
- Keep all browser downloads inside the image build so `docker compose up -d --build` is sufficient.

Implementation notes:
- Group the browser install commands together in the existing RUN layer.
- Add a short comment clarifying why both browser installers are needed.
- Avoid adding startup-time side effects or entrypoint logic if a build-time install is enough.

Acceptance:
- Fresh image build includes both stock Playwright and rebrowser Chromium runtimes.
- No manual `docker compose exec ... rebrowser_playwright install chromium` step is required after build.

### Task 3: Add a lightweight verification guard for maintainers

Objective: make regressions easier to catch than they were in the audit.

Files:
- Modify: `Dockerfile` or `README.md`
- Possibly create: `scripts/` helper only if genuinely needed

Preferred path:
- Keep this lightweight: document an exact verification command sequence in `README.md` rather than introducing heavy test harness code unless execution proves docs are insufficient.

Possible verification options during implementation review:
- Option A: add a Dockerfile build-time sanity command that imports/queries browser installers after download.
- Option B: document post-build checks in `README.md` and rely on manual smoke validation.

Recommendation:
- Start with README verification instructions; only add Dockerfile sanity assertions if the image can cheaply prove browser presence without brittle path assumptions.

Acceptance:
- Future agents can tell, from repo docs alone, how to rebuild and verify both dynamic modes.

### Task 4: Document the dynamic-stealth runtime contract

Objective: make the fix discoverable and prevent future tribal-knowledge regressions.

Files:
- Modify: `README.md:124-149`
- Possibly modify: top-level troubleshooting section if a better location exists nearby

Planned doc content:
- Docker image installs:
  - Playwright Chromium for standard `fetcher: dynamic`
  - rebrowser Chromium for `fetcher: dynamic` with `browser.stealth: true`
  - Camoufox assets for `fetcher: stealthy`
- Rebuild command:
  - `docker compose up -d --build --force-recreate scrapeyard`
- Smoke verification commands for both dynamic modes.
- A note that fixing this only restores local runtime correctness; it does not guarantee hostile-site access success.

Acceptance:
- README explains the three browser-runtime families clearly enough that a new maintainer would not misdiagnose this again.

### Task 5: Validate the fixed image with explicit in-container smoke probes

Objective: prove the slice resolves the actual defect.

Files:
- No permanent file change required unless a helper config is added
- Use existing Docker/compose setup

Execution verification plan:
1. Rebuild image:
   - `docker compose up -d --build --force-recreate scrapeyard`
2. Verify browser installers are present in the running container.
3. Run one minimal dynamic smoke probe with `browser.stealth: false`.
4. Run one minimal dynamic smoke probe with `browser.stealth: true`.
5. Confirm the stealth=true probe no longer fails with:
   - `BrowserType.launch: Executable doesn't exist at /root/.cache/ms-playwright/chromium-1169/chrome-linux/chrome`

Suggested probe target:
- use a benign simple page such as `https://example.com` or another low-friction endpoint; this slice is about runtime startup, not hostile-site success.

Acceptance:
- Both probes execute in-container.
- The prior rebrowser executable error is gone.
- Any remaining failures, if present, are now real fetch/runtime issues rather than missing browser binaries.

### Task 6: Add or update narrow automated coverage only if it materially helps

Objective: avoid fake coverage while still capturing any cheap, durable checks.

Files:
- Possibly modify: `tests/unit/test_scraper_decomposition.py`
- Possibly add: a small test file only if there is a meaningful pure-Python invariant to lock down

Guidance:
- Do not add brittle tests that assert opaque browser cache revision numbers.
- Prefer tests only for repo-owned logic, not third-party installer internals.
- If no meaningful automated check exists beyond docs + smoke validation, skip new tests for this slice.

Acceptance:
- Any added tests are stable and repo-owned.
- No test is added solely to pretend the Docker runtime problem is unit-tested.

---

## Files likely to change

Primary:
- `Dockerfile`
- `README.md`

Possible but optional:
- `tests/unit/test_scraper_decomposition.py`
- a small helper script under `scripts/` only if smoke verification needs a reusable wrapper

Files unlikely to change:
- `src/scrapeyard/engine/browser_debug.py`
- `src/scrapeyard/engine/scraper.py`
- `src/scrapeyard/config/schema.py`

---

## Validation plan

Static checks after code changes:
- `poetry run ruff check src tests`
- `poetry run pytest tests/unit/test_scraper_decomposition.py -q`
- `poetry run pytest -q`

Docker/runtime checks:
- `docker compose up -d --build --force-recreate scrapeyard`
- container-level check for Playwright browser availability
- container-level check for rebrowser browser availability
- minimal dynamic scrape with `stealth: false`
- minimal dynamic scrape with `stealth: true`

Success definition:
- the image rebuild alone restores the dynamic-stealth browser runtime
- docs tell maintainers how to verify it
- the known missing-executable defect is no longer reproducible in the rebuilt container

---

## Risks and tradeoffs

Risks:
- browser downloads increase image build time and image size
- build-time verification may become brittle if it depends on exact cache paths or revision numbers
- `rebrowser-playwright` CLI/module invocation may differ from assumptions and needs live confirmation during execution

Tradeoffs:
- build-time installation is heavier than a manual post-start fix, but much more reproducible
- documentation-only verification is cheaper than adding new automation, but requires disciplined manual smoke checks

---

## Open questions for execution

1. Is `rebrowser_playwright install chromium` or `python -m rebrowser_playwright install chromium` the best build-time invocation in this image?
2. Can the Docker build cheaply verify rebrowser browser presence without hard-coding revision-specific cache paths?
3. Is a reusable smoke config/helper worth adding, or are README commands enough for this narrow slice?

---

## Recommended next move

Execute Tasks 1-5 as a narrow slice. Do not combine this with observability or classifier work yet. The best outcome of this slice is simple: the Docker image reliably starts both dynamic browser modes, and future hostile-retailer investigations stop tripping over a missing rebrowser binary.