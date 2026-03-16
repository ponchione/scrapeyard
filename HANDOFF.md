# Scrapeyard — Handoff

**Last updated:** 2026-03-16
**Branch:** main (all work merged)

## Project overview

Scrapeyard is a config-driven web scraping microservice (FastAPI + Scrapling). Users submit YAML configs defining targets, selectors, and execution rules. Jobs run async via a worker pool or sync for simple single-target requests. Results persist to the local filesystem with metadata in SQLite. Optional webhooks notify consumers on job completion.

## Current state

The core service is functional with webhook support wired end-to-end. Consumer projects (e.g. EyeBox) own their own configs and submit them via the API.

| WO | Title | Summary |
|----|-------|---------|
| 000 | Fix ad-hoc job UNIQUE constraint | Ad-hoc jobs get UUID-suffixed names to avoid collision |
| 001 | WebhookConfig schema | `WebhookConfig` model + `WebhookStatus` enum on `ScrapeConfig` |
| 002 | SaveResultMeta return type | `save_result` returns frozen `SaveResultMeta(run_id, file_path, record_count)` instead of bare string |
| 003 | Webhook dispatcher module | `src/scrapeyard/webhook/` — Protocol, httpx dispatcher (fire-and-forget), payload builder, `should_fire` filter |
| 004 | Wire webhook into worker | `scrape_task` accepts `webhook_dispatcher`, fires via `asyncio.create_task` after save; sync path skips; integration tested |

## Key architecture decisions

- **Webhook dispatch is fire-and-forget.** Exceptions are caught and logged at WARNING inside `HttpWebhookDispatcher.dispatch`. The `asyncio.create_task` in the worker ensures dispatch never blocks status updates.
- **Sync path (`POST /scrape`) does not fire webhooks.** The sync route doesn't pass `webhook_dispatcher` to `scrape_task`, so the kwarg defaults to `None` and the webhook block is skipped. This is by design — sync results are returned inline.
- **`record_count` is caller-supplied.** The worker passes `len(flat_data)` (raw scraped records) rather than having `save_result` compute it from the formatted envelope. This gives accurate counts regardless of output format.
- **`SaveResultMeta` is a frozen dataclass**, not Pydantic. Kept lightweight since it's an internal return type.

## Package structure

```
src/scrapeyard/
├── api/            # FastAPI routes + dependency injection
├── common/         # Settings, shared utilities
├── config/         # YAML schema (Pydantic), loader
├── engine/         # Scraper, resilience (circuit breaker, validation)
├── formatters/     # Output formatting (JSON, markdown, HTML)
├── models/         # Domain models (Job, ErrorRecord)
├── queue/          # Worker pool, scrape_task orchestration
├── scheduler/      # Cron scheduling (APScheduler)
├── storage/        # Result store, job store, error store (SQLite + filesystem)
└── webhook/        # Dispatcher (Protocol + httpx), payload builder, should_fire
```

## Known issues

- 2 pre-existing test failures (APScheduler `jitter` kwarg mismatch, Scrapling `auto_save` key change) — unrelated to any work order
- Queue backend uses `asyncio.PriorityQueue` instead of arq (see `docs/TECH-DEBT.md`)

## Test suite

```
.venv/bin/python -m pytest tests/ -v
```
