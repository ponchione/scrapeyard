# Scrapeyard

Scrapeyard is a config-driven web scraping service built with FastAPI,
Scrapling, Redis workers, APScheduler, SQLite, and local result artifacts.

It is useful when you want a small service around scraping jobs instead of a
collection of one-off scripts. Clients submit YAML, Scrapeyard validates it,
queues the work, runs it through the worker path, stores results, and exposes
job state over HTTP.

## Features

- Declarative YAML scrape configs
- Ad hoc and cron-scheduled jobs
- Sync, async, and auto response modes
- One queued execution path for all scrapes
- Multi-target jobs with concurrency and rate-limit controls
- Scrapling `basic`, `stealthy`, and `dynamic` fetchers
- Item-scoped extraction, typed pagination, selector transforms, and validation rules
- Browser actions for consent clicks, waits, scrolling, and load-more buttons
- JSON result artifacts stored on disk and indexed in SQLite
- Job, run, error, result, and health APIs
- Durable webhook outbox with retry
- Docker Compose setup with Redis

## Repository Layout

```text
src/scrapeyard/
  api/          FastAPI routes, middleware, serializers, dependencies
  common/       Settings, logging, IDs, time helpers
  config/       YAML loading, schema, transforms
  engine/       Scrapling integration, selectors, resilience
  models/       Shared domain models
  queue/        Redis enqueueing, worker pool, task execution
  runtime/      Health probes
  scheduler/    APScheduler cron integration
  storage/      SQLite stores and filesystem persistence
  webhook/      Outbound webhook delivery

sql/            SQLite schema scripts
tests/          Unit, integration, and live Redis tests
examples/       Example scrape configs
```

## Requirements

- Python 3.10+
- Poetry
- Redis for real app runs
- Docker and Docker Compose for the bundled local stack

## Quick Start

Install dependencies:

```bash
poetry install
```

Run Redis locally or point `SCRAPEYARD_REDIS_DSN` at an existing Redis
instance. For local runs outside Docker, override the default `/data/...` paths
to writable directories:

```bash
export SCRAPEYARD_API_KEY="$(openssl rand -hex 32)"
export SCRAPEYARD_API_KEYS="$SCRAPEYARD_API_KEY"
export SCRAPEYARD_DB_DIR=/tmp/scrapeyard/db
export SCRAPEYARD_STORAGE_RESULTS_DIR=/tmp/scrapeyard/results
export SCRAPEYARD_ADAPTIVE_DIR=/tmp/scrapeyard/adaptive
export SCRAPEYARD_LOG_DIR=/tmp/scrapeyard/logs
poetry run uvicorn scrapeyard.main:app --host 0.0.0.0 --port 8420
```

Check health:

```bash
curl -s http://127.0.0.1:8420/health
```

## Docker

Start the full local stack:

```bash
export SCRAPEYARD_API_KEY="$(openssl rand -hex 32)"
export SCRAPEYARD_API_KEYS="$SCRAPEYARD_API_KEY"
docker compose up -d --build
```

Stop it:

```bash
docker compose down
```

The Compose setup starts Scrapeyard and Redis, mounts persistent data at
`/data`, and expects `SCRAPEYARD_API_KEYS` from the shell or a local `.env`
file.

When browser-runtime dependencies change, rebuild the app container:

```bash
docker compose up -d --build --force-recreate scrapeyard
```

The image installs Playwright Chromium for standard `fetcher: dynamic` jobs,
rebrowser Chromium for `fetcher: dynamic` with `browser.stealth: true`, and
Camoufox assets for `fetcher: stealthy`. The container refreshes mounted
volume ownership on startup, restores Chromium sandbox permissions, and then
runs the app as a non-root user. The local Compose file sets
`security_opt: [seccomp:unconfined]` because the rebrowser Chromium sandbox
needs namespace syscalls that Docker's default seccomp profile may block.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Service health and runtime probes |
| `POST` | `/scrape` | Submit an ad hoc scrape |
| `POST` | `/jobs` | Register a scheduled job |
| `GET` | `/jobs` | List jobs |
| `GET` | `/jobs/{job_id}` | Read job details and run state |
| `DELETE` | `/jobs/{job_id}` | Delete a job |
| `GET` | `/results/{job_id}` | Read stored results |
| `GET` | `/errors` | Query stored errors |

Non-health endpoints require `X-API-Key` when `SCRAPEYARD_API_KEYS` is set.

## Example Config

```yaml
project: demo
name: example-job

target:
  url: https://example.com
  fetcher: basic
  selectors:
    title: h1
```

Submit it:

```bash
curl -sS \
  -H "X-API-Key: $SCRAPEYARD_API_KEY" \
  -H "Content-Type: application/x-yaml" \
  --data-binary @examples/basic-scrape.yaml \
  http://127.0.0.1:8420/scrape
```

More examples:

- [examples/basic-scrape.yaml](examples/basic-scrape.yaml)
- [examples/dynamic-product-grid.yaml](examples/dynamic-product-grid.yaml)
- [examples/dynamic-consent-scroll.yaml](examples/dynamic-consent-scroll.yaml)
- [examples/load-more-product-grid.yaml](examples/load-more-product-grid.yaml)
- [examples/scheduled-product-monitor.yaml](examples/scheduled-product-monitor.yaml)
- [template.yaml](template.yaml)

## Configuration Notes

Exactly one of `target` or `targets` is required.

Common target fields:

| Field | Purpose |
| --- | --- |
| `url` | Target URL |
| `fetcher` | `basic`, `stealthy`, or `dynamic` |
| `selectors` | Output fields mapped to CSS or XPath selectors |
| `item_selector` | Optional repeated-item container selector |
| `pagination` | Optional next-page selector and page limit |
| `browser` | Optional browser runtime controls and pre-extraction actions for dynamic fetches |
| `proxy` | Optional per-target proxy override |
| `map_detection` | Optional pricing visibility detection rules |
| `stock_detection` | Optional stock status detection rules |

Selector transforms can be chained with `|`. Supported string transforms are
`trim`, `collapse_whitespace`, `lowercase`, `uppercase`, `prepend`, `append`,
`replace`, `remove`, `strip_prefix`, `strip_suffix`, `regex`, `extract`, and
`default`.

Example transform chain:

```yaml
selectors:
  title:
    query: ".product-title::text"
    transform: "trim|collapse_whitespace"
  price:
    query: ".price::text"
    transform: 'trim|strip_prefix("$")|remove(",")'
```

Pagination accepts the same short-form CSS selector style as selectors, or a
long-form selector when XPath is needed:

```yaml
pagination:
  next:
    query: "//a[contains(., 'Next')]"
    type: xpath
  max_pages: 5
```

Browser-backed targets may define an ordered `browser.actions` list with
`click`, `wait_for_selector`, `wait_ms`, `scroll`, and `repeat_click` actions.
Use hard limits such as `times` or `max_times` on repeating actions.

Example browser actions:

```yaml
browser:
  actions:
    - type: click
      selector: "#accept-cookies"
      optional: true
      timeout_ms: 3000
      wait_ms: 500
    - type: wait_for_selector
      selector: ".product-card"
      timeout_ms: 15000
    - type: scroll
      times: 3
      pixels: 1000
      wait_ms: 500
    - type: repeat_click
      selector: "button.load-more"
      max_times: 8
      wait_for_selector: ".product-card"
      wait_ms: 750
      optional: true
```

Common job fields:

| Field | Purpose |
| --- | --- |
| `project` | Project namespace |
| `name` | Job name |
| `targets` | Multi-target scrape definition |
| `schedule` | Cron schedule for `POST /jobs` |
| `execution` | Mode, priority, concurrency, delay, and fail strategy |
| `retry` | Retry policy |
| `validation` | Required fields, minimum result count, empty-result action |
| `output` | Result grouping |
| `webhook` | Completion notification target |

## Settings

All service settings use the `SCRAPEYARD_` prefix. The most commonly changed
settings are:

| Variable | Default | Purpose |
| --- | --- | --- |
| `SCRAPEYARD_API_KEYS` | empty | Comma-separated API key allow-list |
| `SCRAPEYARD_REDIS_DSN` | `redis://redis:6379/0` | Redis connection for `arq` |
| `SCRAPEYARD_QUEUE_NAME` | `scrapeyard` | Redis queue name |
| `SCRAPEYARD_DB_DIR` | `/data/db` | SQLite database directory |
| `SCRAPEYARD_STORAGE_RESULTS_DIR` | `/data/results` | Result artifact directory |
| `SCRAPEYARD_ADAPTIVE_DIR` | `/data/adaptive` | Scrapling adaptive state directory |
| `SCRAPEYARD_LOG_DIR` | `/data/logs` | Log directory |
| `SCRAPEYARD_SYNC_TIMEOUT_SECONDS` | `15` | Max wait for sync scrape responses |
| `SCRAPEYARD_SYNC_POLL_DELAY_SECONDS` | `0.5` | Sync response polling interval |
| `SCRAPEYARD_WORKERS_MAX_CONCURRENT` | `4` | Max concurrent jobs |
| `SCRAPEYARD_WORKERS_MAX_BROWSERS` | `2` | Max concurrent browser jobs |
| `SCRAPEYARD_MAX_REQUEST_BYTES` | `262144` | Max request body size |

See [src/scrapeyard/common/settings.py](src/scrapeyard/common/settings.py) for
the full settings surface.

## Testing

Fast checks:

```bash
poetry run ruff check src tests
poetry run pytest
```

Live Redis queue-path checks:

```bash
./scripts/run_live_redis_tests.sh
```

The live Redis runner starts an isolated Redis container on port `56379`, runs
the `live_redis` tests, and tears the container down. Regular `pytest` runs
skip those tests when that Redis instance is unavailable.

See [docs/TESTING.md](docs/TESTING.md) for the testing lanes.

## Deployment Notes

- Set `SCRAPEYARD_API_KEYS` before exposing non-health endpoints.
- Keep port `8420` private. Scrapeyard is designed to be consumed by Eyebox or
  another trusted internal service, not exposed as a public API.
- Use persistent storage for `/data` and Redis append-only data.
- Treat the current service as single-instance. The queue is Redis-backed, but
  SQLite stores and local result artifacts are not a horizontally scaled
  deployment model.
- Store secrets in environment variables or an orchestrator secret store.
- Follow [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) before promoting a runtime
  environment.
