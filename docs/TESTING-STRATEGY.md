# Scrapeyard Testing Strategy

Last updated: 2026-03-19

## Goal

Validate that Scrapeyard still behaves correctly after the 2026-03-18 debt
remediation, with special focus on the changes that are easy to miss in mocked
tests:

- Redis-backed durable queueing
- `POST /scrape` always executing through the queue/worker path
- cleanup flowing through the `ResultStore` contract
- browser tuning and `adaptive_domain` working as first-class config

## Current Baseline

- Static checks: `poetry run ruff check src tests`
- Automated tests: `poetry run pytest -q`
- Live Redis lane: `./scripts/run_live_redis_tests.sh`
- Current result on 2026-03-19: `198 passed, 3 skipped`

The fast confidence layer still uses the monkeypatched integration harness, but
Scrapeyard now also has a dedicated live Redis automation lane for real queue
coverage.

## Test Layers

### 1. Fast Local Regression

Run on every change and before any manual QA session.

- `poetry run ruff check src tests`
- `poetry run pytest tests/unit`
- `poetry run pytest tests/integration`
- `poetry run pytest -q`

Purpose:

- Protects validation, storage, scheduler, formatting, cleanup, and route
  behavior.
- Confirms sync-wait semantics at the API level.
- Does not prove Redis connectivity, queue durability, or embedded `arq`
  execution.

### 2. Real Service Validation

Run against a live Scrapeyard instance with real Redis before Eyeboxapp-driven
testing.

Recommended setup:

- Preferred: `docker compose up -d`
- Alternate local run: start Redis separately, then run Scrapeyard with
  writable local overrides for `SCRAPEYARD_DB_DIR`,
  `SCRAPEYARD_STORAGE_RESULTS_DIR`, `SCRAPEYARD_ADAPTIVE_DIR`, and
  `SCRAPEYARD_LOG_DIR`

Smoke checks:

- `GET /health` returns `200` with `status: ok`
- Async `POST /scrape` returns `202` and a `job_id`
- `GET /results/{job_id}` returns `202` while queued/running, then `200`
- Sync `POST /scrape` returns `200` for a fast single-target basic job
- `POST /jobs` creates a scheduled job, `GET /jobs` lists it, `DELETE /jobs/{id}`
  removes it

Purpose:

- Proves the app can start with Redis, SQLite, scheduler, and filesystem
  persistence all wired together.
- Proves ad hoc work uses the real queue path, not the integration-test fake.

### 2a. Live Redis Automation

Run the dedicated real queue-path suite:

- `./scripts/run_live_redis_tests.sh`

This lane:

- starts an isolated Redis container on a test-only port
- exercises the real `WorkerPool` and embedded `arq` worker
- verifies async completion, sync-over-queue completion, and error persistence

This is the primary automated guardrail for the Redis migration.

### 3. Eyeboxapp End-to-End Validation

Use Eyeboxapp as the external client and operator surface. This layer should
prove the full contract from another system's point of view.

Run these scenarios in order:

1. Health and connectivity
- Confirm Eyeboxapp can reach `GET /health`
- Confirm Redis-backed Scrapeyard survives restart and comes back healthy

2. Basic sync scrape
- Use a single-target `fetcher: basic` config with `execution.mode: sync`
- Expected result: `POST /scrape` returns `200`, job status becomes
  `complete`, and results are immediately available

3. Basic async scrape
- Use the same target with `execution.mode: async`
- Expected result: `POST /scrape` returns `202`, Eyeboxapp polls
  `/results/{job_id}`, and final results arrive with `200`

4. Dynamic/browser scrape
- Use `fetcher: dynamic` and a `browser` block
- Expected result: successful completion and correct extraction from a page
  requiring browser execution

5. Item-scoped extraction
- Use `item_selector` plus relative selectors
- Expected result: one structured record per repeated item container, not
  page-wide parallel arrays

6. Pagination
- Use a target with `pagination.next` and `max_pages > 1`
- Expected result: multiple pages scraped with merged results and no duplicate
  or missing page transitions

7. Scheduled job lifecycle
- Create a scheduled job through `POST /jobs`
- Wait for a real trigger or use a short cron during the session
- Expected result: `run_count` increments and a fresh result artifact appears

8. Webhook delivery
- Configure Scrapeyard webhook delivery to an Eyeboxapp-controlled endpoint if
  available
- Expected result: webhook fires on completion with `job_id`, `project`,
  `event`, `run_id`, and `results_url`

9. Error visibility
- Trigger a known-bad selector set or an unreachable target
- Expected result: job ends `failed` or `partial`, and `/errors?job_id=...`
  shows actionable error records

10. Restart and durability
- Submit an async scrape, restart Scrapeyard during or immediately after queue
  submission, then inspect job state
- Expected result: queued work is not silently lost; final state is visible via
  jobs/results after recovery

## High-Risk Areas To Target

These should be treated as release gates for the recent debt paydown.

- Queue durability: jobs survive API process restart because queue state lives
  in Redis
- Sync-over-queue semantics: sync requests still pass through Redis and only
  wait on completion
- Duplicate ad hoc submission safety: repeated `POST /scrape` calls do not
  collide on identifiers or names
- Browser gating: dynamic jobs respect worker browser concurrency limits
- Cleanup correctness: retention and per-job result pruning delete metadata and
  artifacts together
- Adaptive settings: `adaptive` and `adaptive_domain` behave correctly across
  repeated runs

## Suggested Session Checklist

- Run `ruff` and `pytest` first
- Start Scrapeyard with real Redis
- Validate `/health`
- Run one sync scrape
- Run one async scrape
- Run one dynamic/browser scrape
- Run one scheduled job
- Run one webhook scenario
- Run one controlled failure and inspect `/errors`
- Run one restart/durability scenario
- Inspect `/data/results` and SQLite-backed job state for the runs you created

## Brownells Two-Phase Plan

Use these fixtures for the Eyeboxapp session:

- Phase 1 smoke: [brownells-optics-smoke.yaml](/home/gernsback/source/scrapeyard/docs/test-configs/brownells-optics-smoke.yaml)
- Phase 2 validation: [brownells-optics-validation.yaml](/home/gernsback/source/scrapeyard/docs/test-configs/brownells-optics-validation.yaml)

Phase 1 goals:

- Prove Eyeboxapp can submit a live async scrape and poll it successfully
- Prove Brownells still works with `fetcher: dynamic`
- Prove item selectors, result persistence, and webhook delivery are functional
- Keep runtime and retailer load low with `max_pages: 1`

Phase 1 expected outcome:

- `POST /scrape` returns `202`
- job transitions `queued -> running -> complete` or `failed`
- `/results/{job_id}` eventually returns `200` on success
- Eyeboxapp receives a webhook for success or failure

Phase 2 goals:

- Exercise the heavier Brownells path with pagination enabled
- Exercise `adaptive: true` with explicit `adaptive_domain: brownells.com`
- Validate result quality and webhook behavior on a larger scrape

Phase 2 expected outcome:

- multiple pages are scraped up to `max_pages: 3`
- at least 5 valid records are captured
- webhook payload includes the final state and result URL
- rerunning the same config should not collide on ad hoc identifiers

Notes:

- Both fixtures assume Scrapeyard is running in Docker, since the webhook URL
  uses `host.docker.internal`
- If Scrapeyard is running directly on the host instead, swap the webhook URL
  to a host-reachable address such as `http://127.0.0.1:19057/ingest`

## Exit Criteria

The session is successful when all of the following are true:

- Automated suite still passes
- Real Redis-backed ad hoc jobs succeed in both sync and async modes
- Eyeboxapp can drive the HTTP contract without special handling
- At least one browser-backed scrape succeeds
- Scheduled execution produces a real run
- Webhook behavior is observed end-to-end
- Failure states are observable in `/jobs`, `/results`, and `/errors`
- Restart testing shows no silent job loss

## Known Gap After This Strategy

The largest remaining automation gap is the browser/system lane. The repo still
lacks a deterministic end-to-end test that exercises the real browser-backed
path, pagination, item extraction, and webhook transport together. The backlog
for that work is tracked in
[TESTING-BACKLOG.md](/home/gernsback/source/scrapeyard/docs/TESTING-BACKLOG.md).
