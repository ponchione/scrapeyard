# Scrapeyard

Scrapeyard is a config-driven web scraping service built with FastAPI and Scrapling. You submit YAML, the service validates it, creates a job, enqueues execution through a Redis-backed durable queue, stores job metadata in SQLite, writes result artifacts to disk, and exposes jobs, results, errors, and health over HTTP.

It is designed for small-to-medium scraping workflows where you want:

- a stable HTTP API instead of one-off scripts
- YAML-defined scraping jobs instead of hard-coded crawlers
- persisted results and error records
- cron-based recurring scrapes
- durable queued execution with Redis and `arq`

## Features

- `POST /scrape` for ad hoc scrape jobs
- `POST /jobs` for cron-scheduled jobs
- sync, async, or auto execution mode
- multi-target jobs with per-job concurrency and rate limits
- `basic`, `stealthy`, and `dynamic` fetcher support via Scrapling
- per-target browser tuning and `adaptive_domain` overrides
- selector transforms such as `trim`, `uppercase`, `prepend(...)`, and `regex(...)`
- pagination support
- result formatting as JSON, Markdown, HTML, or JSON+Markdown
- webhook notifications on job completion (fire-and-forget)
- SQLite-backed job, error, and result metadata stores
- filesystem-backed result artifacts
- per-domain circuit breaker and retry handling
- background cleanup for retention and per-job result pruning

## Architecture

At runtime the service is composed of:

- FastAPI application and HTTP routes
- a Redis-backed `arq` queue with one queued execution path for all jobs
- a local worker pool that executes queued jobs with bounded shutdown drain
- APScheduler for cron-based scheduled execution
- SQLite databases for jobs, errors, and result metadata
- a local results directory for run artifacts

Request flow:

1. A YAML config is submitted to `POST /scrape` or `POST /jobs`.
2. The config is validated with Pydantic models.
3. A job record is written to `jobs.db`.
4. The job is enqueued on Redis with a per-run delivery ID.
5. `POST /scrape` may wait on queued completion up to `SCRAPEYARD_SYNC_TIMEOUT_SECONDS`.
6. The worker executes targets, applies retries, rate limiting, validation, and formatting.
7. Results are written to disk and indexed in `results_meta.db`.
8. Errors are written to `errors.db`.
9. If a `webhook` is configured, a JSON payload is POSTed to the webhook URL (fire-and-forget).

## Repository Layout

- `src/scrapeyard/main.py`: FastAPI app and lifespan wiring
- `src/scrapeyard/api/routes.py`: HTTP API
- `src/scrapeyard/config/schema.py`: YAML schema
- `src/scrapeyard/engine/`: scraping, selectors, retries, validation, circuit breaker
- `src/scrapeyard/queue/`: Redis queue integration, worker pool, and scrape task
- `src/scrapeyard/scheduler/cron.py`: APScheduler integration
- `src/scrapeyard/storage/`: SQLite and filesystem persistence
- `src/scrapeyard/webhook/`: webhook dispatcher (Protocol + httpx), payload builder
- `sql/`: SQLite schema creation scripts
- `tests/`: unit and integration tests

## Testing

Fast local checks:

```bash
poetry run ruff check src tests
poetry run pytest -q
```

Live Redis queue-path automation:

```bash
./scripts/run_live_redis_tests.sh
```

This runner starts an isolated Redis container on host port `56379`, runs the
`live_redis` pytest marker, then tears the container down. During a normal
`poetry run pytest`, these tests skip cleanly if that Redis instance is not
available.

Testing references:

- Strategy: [docs/TESTING-STRATEGY.md](/home/gernsback/source/scrapeyard/docs/TESTING-STRATEGY.md)
- Remaining automation backlog: [docs/TESTING-BACKLOG.md](/home/gernsback/source/scrapeyard/docs/TESTING-BACKLOG.md)
- Brownells manual fixtures: [docs/test-configs/brownells-optics-smoke.yaml](/home/gernsback/source/scrapeyard/docs/test-configs/brownells-optics-smoke.yaml) and [docs/test-configs/brownells-optics-validation.yaml](/home/gernsback/source/scrapeyard/docs/test-configs/brownells-optics-validation.yaml)

## Requirements

- Python `3.10+`
- Poetry
- Docker and Docker Compose optional, but supported

## Run Locally

Install dependencies:

```bash
poetry install
```

Make sure Redis is running locally, or point `SCRAPEYARD_REDIS_DSN` at an
existing Redis instance before starting the API.

Start the API:

```bash
poetry run uvicorn scrapeyard.main:app --host 0.0.0.0 --port 8420
```

When running outside Docker, override the default `/data/...` paths to writable
local directories before starting the service.

Health check:

```bash
curl -s http://127.0.0.1:8420/health
```

## Run with Docker

Build and start:

```bash
docker compose build
docker compose up -d
```

Check health:

```bash
curl -s http://127.0.0.1:8420/health
```

Stop:

```bash
docker compose down
```

The default compose setup starts both Redis and Scrapeyard. It mounts a named
volume at `/data` for databases, results, adaptive matching state, and logs,
and a second named volume for Redis append-only durability. The Docker image
also installs Playwright Chromium so `fetcher: dynamic` works inside the
container.

## Environment Variables

All service settings use the `SCRAPEYARD_` prefix.

| Variable | Default | Purpose |
| --- | --- | --- |
| `SCRAPEYARD_REDIS_DSN` | `redis://redis:6379/0` | Redis connection used by `arq` |
| `SCRAPEYARD_QUEUE_NAME` | `scrapeyard` | Redis queue name |
| `SCRAPEYARD_WORKERS_MAX_CONCURRENT` | `4` | Max concurrent jobs in the worker pool |
| `SCRAPEYARD_WORKERS_MAX_BROWSERS` | `2` | Max concurrent browser-based jobs |
| `SCRAPEYARD_WORKERS_MEMORY_LIMIT_MB` | `4096` | Reject new jobs when RSS exceeds this limit |
| `SCRAPEYARD_SYNC_TIMEOUT_SECONDS` | `15` | How long sync mode waits on queued completion |
| `SCRAPEYARD_WORKERS_SHUTDOWN_GRACE_SECONDS` | `30` | Grace period for worker drain on shutdown |
| `SCRAPEYARD_WORKERS_RUNNING_LEASE_SECONDS` | `300` | Lease used to recover stale running jobs |
| `SCRAPEYARD_SCHEDULER_JITTER_MAX_SECONDS` | `120` | Random jitter applied to scheduled jobs |
| `SCRAPEYARD_STORAGE_RETENTION_DAYS` | `30` | Age-based result retention |
| `SCRAPEYARD_STORAGE_RESULTS_DIR` | `/data/results` | Root directory for result artifacts |
| `SCRAPEYARD_STORAGE_MAX_RESULTS_PER_JOB` | `100` | Per-job run history cap |
| `SCRAPEYARD_DB_DIR` | `/data/db` | Directory containing SQLite databases |
| `SCRAPEYARD_ADAPTIVE_DIR` | `/data/adaptive` | Scrapling adaptive storage directory |
| `SCRAPEYARD_LOG_DIR` | `/data/logs` | Service log directory |
| `SCRAPEYARD_CIRCUIT_BREAKER_MAX_FAILURES` | `3` | Failures before a domain breaker opens |
| `SCRAPEYARD_CIRCUIT_BREAKER_COOLDOWN_SECONDS` | `300` | Breaker cooldown before retrying a domain |

Example:

```bash
SCRAPEYARD_DB_DIR=/tmp/scrapeyard/db \
SCRAPEYARD_STORAGE_RESULTS_DIR=/tmp/scrapeyard/results \
SCRAPEYARD_ADAPTIVE_DIR=/tmp/scrapeyard/adaptive \
SCRAPEYARD_LOG_DIR=/tmp/scrapeyard/logs \
poetry run uvicorn scrapeyard.main:app --host 0.0.0.0 --port 8420
```

## YAML Configuration

Exactly one of `target` or `targets` is required.

A reusable starter config is available at [`template.yaml`](/home/gernsback/source/scrapeyard/template.yaml).

### Minimal ad hoc config

```yaml
project: demo
name: example-job

target:
  url: https://example.com
  fetcher: basic
  selectors:
    title: h1
```

### Multi-target job

```yaml
project: demo
name: product-recon

targets:
  - url: https://example.com/products
    fetcher: basic
    selectors:
      name: .product-card h2::text
      price: .price::text

  - url: https://example.com/blog
    fetcher: basic
    selectors:
      title: article h2 a::text
      href: article h2 a::attr(href)
```

### Item-scoped extraction

```yaml
project: demo
name: product-cards

target:
  url: https://example.com/products
  fetcher: basic
  item_selector: .product-card
  selectors:
    name: .title::text
    price: .price::text
    url:
      query: a::attr(href)
      transform: prepend("https://example.com")
```

When `item_selector` is set, Scrapeyard matches each repeated item container and
applies the field selectors relative to that item, returning one record per
match instead of page-wide parallel arrays.
This is the preferred mode for product grids and other repeated card layouts.

### Raw vs Generated Fields

Selectors should populate raw retailer fields such as `name`, `price`, `url`,
`stock_signal`, `manufacturer`, `sku`, `mpn`, and `upc` when those values are
actually exposed on the listing card or page you are scraping. Do not invent
selectors for identifier fields that are only available deeper in the retailer
flow.

`stock_status` is a system-generated detection field; if you want to keep the
raw extracted availability text, map it to a selector such as `stock_signal`.
Scrapeyard checks `stock_signal` first, then falls back to DOM text and CSS
selectors during stock detection. For backward compatibility, legacy raw
`stock_status` selector output is copied into `stock_signal` during enrichment
only when `stock_signal` is missing or blank. New configs should use
`stock_signal` directly.
Scrapeyard also generates `pricing_visibility` and `display_price_text` during
MAP detection. Numeric prices are always classified as `explicit`; use
`map_detection` for non-numeric sentinel values and retailer cues instead of
raw selector keys in new configs.

### Scheduled job

```yaml
project: demo
name: scheduled-products

schedule:
  cron: "*/15 * * * *"
  enabled: true

target:
  url: https://example.com/products
  fetcher: basic
  selectors:
    name: .product-card h2::text
    price: .price::text
```

### Pagination and transforms

```yaml
project: demo
name: paged-quotes

target:
  url: http://quotes.toscrape.com/
  fetcher: basic
  selectors:
    quote:
      query: ".quote .text::text"
      transform: trim
    author:
      query: ".quote .author::text"
      transform: uppercase
    about_url:
      query: ".quote span a::attr(href)"
      transform: prepend("http://quotes.toscrape.com")
  pagination:
    next: li.next a
    max_pages: 3
```

### Top-level fields

| Field | Type | Notes |
| --- | --- | --- |
| `project` | string | Project namespace used in job records and result paths |
| `name` | string | Job name within the project |
| `target` | object | Single-target config |
| `targets` | list | Multi-target config |
| `adaptive` | bool | Overrides adaptive mode; otherwise defaults by job type |
| `retry` | object | Retry policy |
| `validation` | object | Result validation behavior |
| `execution` | object | Concurrency, rate limit, priority, and mode |
| `schedule` | object | Required for `POST /jobs` |
| `webhook` | object | Optional completion notifications |
| `output` | object | Format and grouping |

### Target fields

| Field | Type | Notes |
| --- | --- | --- |
| `url` | string | Target URL |
| `fetcher` | enum | `basic`, `stealthy`, `dynamic` |
| `adaptive_domain` | string | Optional adaptive fingerprint namespace override |
| `browser` | object | Optional timeout/resource/wait tuning for browser-backed fetchers |
| `item_selector` | selector | Optional repeated-item container selector; field selectors run relative to each matched item |
| `selectors` | map | Field name to selector |
| `pagination` | object | Optional pagination config |

Selectors can be short-form strings:

```yaml
selectors:
  title: h1
```

Or long-form objects:

```yaml
selectors:
  title:
    query: h1
    type: css
    transform: trim
```

`type` supports `css` and `xpath`.

### Retry settings

```yaml
retry:
  max_attempts: 3
  backoff: exponential
  backoff_max: 30
  retryable_status: [429, 500, 502, 503, 504]
```

`backoff` supports `exponential`, `linear`, and `fixed`.

### Validation settings

```yaml
validation:
  required_fields: [title, price]
  min_results: 10
  on_empty: warn
```

`on_empty` supports `retry`, `warn`, `fail`, and `skip`.

### Execution settings

```yaml
execution:
  concurrency: 2
  delay_between: 2
  domain_rate_limit: 3
  mode: auto
  priority: normal
  fail_strategy: partial
```

Values:

- `mode`: `auto`, `sync`, `async`
- `priority`: `high`, `normal`, `low`
- `fail_strategy`: `partial`, `all_or_nothing`, `continue`

### Output settings

```yaml
output:
  format: json
  group_by: target
```

Values:

- `format`: `json`, `markdown`, `html`, `json+markdown`
- `group_by`: `target`, `merge`

### Webhook settings

```yaml
webhook:
  url: https://myservice.com/ingest
  on: [complete, partial]
  headers:
    Authorization: "Bearer token123"
  timeout: 10
```

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `url` | string | required | Endpoint to POST results to |
| `on` | list | `[complete, partial]` | Which job statuses trigger the webhook (`complete`, `partial`, `failed`) |
| `headers` | map | `{}` | Custom headers sent with the POST |
| `timeout` | int | `10` | Request timeout in seconds |

Webhooks fire from the worker path for any job that reaches a configured
terminal state, including ad hoc scrapes submitted in sync-wait mode.
Dispatch is fire-and-forget: failures are logged at WARNING but do not affect
job status.

If Scrapeyard is running in Docker and your consumer is running on the host,
use `http://host.docker.internal:<port>/...` rather than `localhost` in the
webhook URL.

The webhook POST body looks like:

```json
{
  "event": "job.complete",
  "job_id": "abc-123",
  "project": "myproject",
  "name": "my-scrape",
  "status": "complete",
  "run_id": "20260316-080130-a1b2c3d4",
  "result_path": "/data/results/myproject/my-scrape/20260316-080130-a1b2c3d4",
  "results_url": "/results/abc-123?run_id=20260316-080130-a1b2c3d4",
  "result_count": 47,
  "error_count": 0,
  "started_at": "2026-03-16T08:00:00Z",
  "completed_at": "2026-03-16T08:01:30Z"
}
```

For ad hoc `POST /scrape` jobs, the stored job directory name includes a random
suffix to avoid collisions. The webhook payload `name` remains the config name,
while `result_path` reflects the suffixed stored directory. Consumers should
key off `payload.name`, not infer identity from `result_path`.

## API

For `POST /scrape` and `POST /jobs`, send `Content-Type: application/x-yaml`.

### `POST /scrape`

Submit an ad hoc scrape job.

```bash
curl -s -X POST http://127.0.0.1:8420/scrape \
  -H 'Content-Type: application/x-yaml' \
  --data-binary @config.yaml
```

Possible responses:

- `200`: the queued job completed within the sync-wait timeout and includes results inline
- `202`: the job was accepted and queued, or sync-wait timed out before completion
- `415`: missing or incorrect content type
- `503`: the server rejected execution or enqueueing because the pool is at capacity

Example sync response:

```json
{
  "job_id": "9a2d4a13-5a0d-4e4e-9eb2-6fa66f5db24a",
  "status": "complete",
  "results": {
    "job_id": "9a2d4a13-5a0d-4e4e-9eb2-6fa66f5db24a",
    "project": "demo",
    "status": "complete",
    "completed_at": "2026-03-12T12:00:00+00:00",
    "errors": [],
    "results": {
      "example.com": {
        "status": "success",
        "count": 1,
        "data": {
          "title": "Example Domain"
        }
      }
    }
  }
}
```

Example async response:

```json
{
  "job_id": "3ab2b237-5f3e-4500-8b8f-b44d9eb63511",
  "status": "queued",
  "poll_url": "/results/3ab2b237-5f3e-4500-8b8f-b44d9eb63511"
}
```

### `GET /results/{job_id}`

Fetch the latest result for a job, or a specific run when `run_id` is provided.

```bash
curl -s "http://127.0.0.1:8420/results/<job_id>"
curl -s "http://127.0.0.1:8420/results/<job_id>?run_id=<run_id>"
```

Behavior:

- `202` while the job is `queued` or `running`
- `200` when results are available
- `400` if `latest=false` is used without `run_id`
- `404` if the job or result does not exist

### `POST /jobs`

Create a scheduled scrape job. The submitted YAML must include a `schedule` block.

```bash
curl -s -X POST http://127.0.0.1:8420/jobs \
  -H 'Content-Type: application/x-yaml' \
  --data-binary @scheduled.yaml
```

Returns `201` with `job_id`, `project`, `name`, and `schedule`.

### `GET /jobs`

List all jobs, or filter by project.

```bash
curl -s "http://127.0.0.1:8420/jobs"
curl -s "http://127.0.0.1:8420/jobs?project=demo"
```

### `GET /jobs/{job_id}`

Fetch a single job record.

```bash
curl -s "http://127.0.0.1:8420/jobs/<job_id>"
```

Returned fields include:

- `job_id`
- `project`
- `name`
- `status`
- `created_at`
- `updated_at`
- `schedule_cron`
- `last_run_at`
- `run_count`

### `DELETE /jobs/{job_id}`

Delete a job and remove its schedule. Optionally delete persisted results too.

```bash
curl -s -X DELETE "http://127.0.0.1:8420/jobs/<job_id>"
curl -s -X DELETE "http://127.0.0.1:8420/jobs/<job_id>?delete_results=true"
```

Returns `204`.

### `GET /errors`

Query structured error records.

```bash
curl -s "http://127.0.0.1:8420/errors"
curl -s "http://127.0.0.1:8420/errors?project=demo"
curl -s "http://127.0.0.1:8420/errors?job_id=<job_id>"
curl -s "http://127.0.0.1:8420/errors?error_type=http_error"
curl -s "http://127.0.0.1:8420/errors?since=2026-03-12T00:00:00"
```

Supported `error_type` values:

- `content_empty`
- `http_error`
- `network_error`
- `browser_error`
- `timeout`

Error records may also include `error_message`, which is useful for diagnosing
browser startup failures, timeouts, and other fetch exceptions.

### `GET /health`

Returns overall status, uptime, worker pool usage, and a per-project summary derived from stored job states.

```bash
curl -s http://127.0.0.1:8420/health
```

## Execution Behavior

### Sync vs async

All ad hoc scrapes are enqueued first. `execution.mode` controls whether the
API waits on queued completion.

When `execution.mode: auto`, `POST /scrape` waits for completion only when all of these are true:

- exactly one target
- no pagination
- `fetcher: basic`

Everything else returns `202` immediately after enqueueing.

You can override this with:

- `execution.mode: sync`: always wait on queued completion up to `SCRAPEYARD_SYNC_TIMEOUT_SECONDS`, then fall back to `202`
- `execution.mode: async`: always return `202` after enqueueing

### Adaptive mode

Adaptive matching defaults to:

- `false` for ad hoc scrapes
- `true` for scheduled jobs

You can override it explicitly with top-level `adaptive: true` or `adaptive: false`.
Adaptive fingerprints are scoped by `project + adaptive_domain`, where
`adaptive_domain` defaults to the normalized target hostname and can be
overridden per target when multiple hostnames should share adaptive state.

### Browser tuning

Browser-backed fetchers keep the current defaults unless a target opts in with:

```yaml
target:
  browser:
    timeout_ms: 90000
    disable_resources: true
    network_idle: false
```

### Failure handling

`execution.fail_strategy` controls the final job status:

- `partial`: mixed success produces `partial`
- `all_or_nothing`: any failed target marks the job `failed` and discards data
- `continue`: keep whatever succeeded; only fully empty jobs become `failed`

### Circuit breaker and retries

- Retry behavior is applied to retryable HTTP statuses from the config
- The circuit breaker is tracked per domain
- Once a domain exceeds the configured failure threshold, that domain is skipped until cooldown expires

## Result Storage

Results are stored on disk beneath:

```text
{SCRAPEYARD_STORAGE_RESULTS_DIR}/{project}/{job_name}/{run_id}/
```

Run IDs look like:

```text
YYYYMMDD-HHMMSS-xxxxxxxx
```

Artifacts written by format:

- `json` -> `results.json`
- `markdown` -> `results.md`
- `html` -> `raw.html`
- `json+markdown` -> `results.json` and `results.md`

Metadata for each run is stored in `results_meta.db`.
When retrieving `json+markdown` results through the API, the service returns the JSON representation and keeps the extra Markdown artifact on disk.

## Scheduled Jobs

Scheduled jobs are persisted in `jobs.db` and re-registered with APScheduler on startup. Cron expressions use standard 5-field crontab syntax, for example:

```yaml
schedule:
  cron: "0 * * * *"
  enabled: true
```

The scheduler applies random jitter up to `SCRAPEYARD_SCHEDULER_JITTER_MAX_SECONDS`.

## Cleanup and Retention

A background cleanup loop runs every 6 hours and:

- deletes result runs older than `SCRAPEYARD_STORAGE_RETENTION_DAYS`
- prunes old runs beyond `SCRAPEYARD_STORAGE_MAX_RESULTS_PER_JOB`

Age-based deletion is applied through the configured result store, and per-job
pruning still removes excess historical runs from both disk and `results_meta.db`.
Job records remain in `jobs.db` unless explicitly deleted.

## Common Errors

- `415 Content-Type must be application/x-yaml`: add `-H 'Content-Type: application/x-yaml'`
- `404 No results found`: the job may still be running or may have failed before saving artifacts; check `GET /jobs/{job_id}` and `GET /errors`
- `503 Service unavailable`: the worker pool rejected the job because the service is at capacity or above its memory limit
- `browser_error` with missing Playwright executable: rebuild the Docker image so the bundled browser runtime is installed

## Development

Run the test suite:

```bash
poetry run pytest
```

Run unit tests only:

```bash
poetry run pytest tests/unit
```

Run integration tests:

```bash
poetry run pytest tests/integration
```

Lint:

```bash
poetry run ruff check src tests
```

## Known Constraints

- Delivery semantics are at-least-once, so idempotent downstream handling is still the right assumption
- The worker pool is local to a single service process, even though queue state is durable in Redis
- `POST /jobs` does not currently enforce YAML content type
- No authentication or authorization layer is present
- Result retrieval returns the latest run unless you already know a historical `run_id`

## License

No license file is present in this repository.
