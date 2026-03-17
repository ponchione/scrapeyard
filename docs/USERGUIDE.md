# Scrapeyard User Guide

Scrapeyard is a config-driven web scraping microservice. You submit YAML configs describing what to scrape, and it handles fetching, parsing, retries, scheduling, and result storage. Results are available via REST API or pushed to your service via webhooks.

**Base URL:** `http://127.0.0.1:8420` (default)

## Quick Start

### Run the service
```bash
# With Poetry
poetry install
poetry run uvicorn scrapeyard.main:app --host 0.0.0.0 --port 8420

# With Docker
docker compose up -d
```

### Submit a scrape
```bash
curl -s -X POST http://127.0.0.1:8420/scrape \
  -H 'Content-Type: application/x-yaml' \
  --data-binary @config.yaml
```

### Verify it's running
```bash
curl -s http://127.0.0.1:8420/health
```

---

## Integration Pattern

Scrapeyard is designed to be consumed by other projects. Each consumer owns its own YAML configs and submits them via the API. There are two integration models:

### Poll-based (simple)
1. `POST /scrape` with your YAML config
2. If `200` — results are inline in the response
3. If `202` — poll `GET /results/{job_id}` until `200`

### Webhook-based (recommended for async/scheduled jobs)
1. Add a `webhook:` block to your YAML config pointing at your ingestion endpoint
2. `POST /jobs` to register a scheduled job, or `POST /scrape` for one-off async
3. Scrapeyard POSTs a JSON payload to your webhook URL on job completion
4. Your service fetches full results via `GET /results/{job_id}?run_id={run_id}`

---

## YAML Config Reference

### Minimal config
```yaml
project: myproject
name: my-scrape
target:
  url: https://example.com
  fetcher: basic
  selectors:
    title: h1
```

### Full config (all options)
```yaml
project: myproject
name: my-scrape

# --- Single target (Tier 1) ---
target:
  url: https://example.com
  fetcher: stealthy          # basic | stealthy | dynamic
  selectors:
    title: "h1::text"
    link:
      query: "a.product::attr(href)"
      type: css              # css | xpath
      transform: prepend("https://example.com")
  pagination:
    next: "a.next-page"
    max_pages: 5

# --- OR multiple targets (Tier 2) ---
# targets:
#   - url: https://site-a.com
#     fetcher: basic
#     selectors:
#       name: ".product-name::text"
#   - url: https://site-b.com
#     fetcher: stealthy
#     selectors:
#       name: "h2.title::text"

adaptive: true               # true | false | null (auto)

retry:
  max_attempts: 3            # default: 3
  backoff: exponential       # exponential | linear | fixed
  backoff_max: 30            # max delay in seconds
  retryable_status:          # default: [429, 500, 502, 503, 504]
    - 429
    - 500
    - 502
    - 503
    - 504

validation:
  required_fields: [title]   # fields that must be non-empty
  min_results: 1             # minimum result count
  on_empty: retry            # retry | warn | fail | skip

execution:
  concurrency: 2             # max simultaneous targets in this job
  delay_between: 2           # seconds between target starts
  domain_rate_limit: 3       # min seconds between requests to same domain
  mode: auto                 # auto | sync | async
  priority: normal           # high | normal | low
  fail_strategy: partial     # partial | all_or_nothing | continue

schedule:
  cron: "0 8 * * MON-FRI"
  enabled: true

webhook:
  url: https://myservice.com/ingest
  on: [complete, partial]    # complete | partial | failed
  headers:                   # optional custom headers
    X-Api-Key: "secret"
  timeout: 10                # seconds

output:
  format: json               # json | markdown | html | json+markdown
  group_by: target           # target | merge
```

### Top-level fields

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `project` | string | Yes | — | Namespace for jobs, results, and errors |
| `name` | string | Yes | — | Unique job name within the project |
| `target` | object | One of `target`/`targets` | — | Single scrape target |
| `targets` | array | One of `target`/`targets` | — | Multiple scrape targets |
| `adaptive` | bool | No | `null` (auto) | Auto: on for scheduled, off for on-demand |
| `retry` | object | No | See below | Retry policy |
| `validation` | object | No | See below | Result validation rules |
| `execution` | object | No | See below | Concurrency and orchestration |
| `schedule` | object | No | `null` | Cron schedule (omit for on-demand) |
| `webhook` | object | No | `null` | Webhook notification config |
| `output` | object | No | See below | Output format and grouping |

### Selector syntax

**Short form** (CSS, most common):
```yaml
selectors:
  title: "h1::text"
  price: ".price-tag::text"
  link: "a.product::attr(href)"
```

**Long form** (explicit type + transform):
```yaml
selectors:
  title:
    query: "//h2[@class='name']/text()"
    type: xpath
    transform: trim
  link:
    query: ".product-link::attr(href)"
    transform: prepend("https://example.com")
```

**Available transforms:**

| Transform | Description | Example |
|-----------|-------------|---------|
| `trim` | Strip whitespace | `trim` |
| `prepend(str)` | Prepend string | `prepend("https://example.com")` |
| `append(str)` | Append string | `append("/details")` |
| `replace(old, new)` | String replacement | `replace("$", "")` |
| `regex(pattern, replacement)` | Regex substitution | `regex("\\d+", "NUM")` |
| `lowercase` | To lowercase | `lowercase` |
| `uppercase` | To uppercase | `uppercase` |
| `join(sep)` | Join multiple results | `join(", ")` |

### Fetcher types

| Fetcher | When to use |
|---------|-------------|
| `basic` | Static HTML pages, no anti-bot protection |
| `stealthy` | Sites with bot detection (uses Camoufox/TLS impersonation) |
| `dynamic` | JavaScript-rendered pages (uses Playwright) |

### Sync vs async behavior

When `execution.mode: auto` (default):
- **Sync (200):** single target, no pagination, `basic` fetcher
- **Async (202):** everything else

Force with `execution.mode: sync` or `execution.mode: async`.

---

## API Reference

All YAML endpoints require `Content-Type: application/x-yaml`.

### POST /scrape

Submit an on-demand scrape.

```bash
curl -s -X POST http://127.0.0.1:8420/scrape \
  -H 'Content-Type: application/x-yaml' \
  --data-binary @config.yaml
```

**200 (sync path):**
```json
{
  "job_id": "abc-123",
  "status": "complete",
  "results": { ... }
}
```

**202 (async path):**
```json
{
  "job_id": "abc-123",
  "status": "queued",
  "poll_url": "/results/abc-123"
}
```

### POST /jobs

Register a scheduled or reusable job. Requires a `schedule` block in the config.

```bash
curl -s -X POST http://127.0.0.1:8420/jobs \
  -H 'Content-Type: application/x-yaml' \
  --data-binary @scheduled.yaml
```

**201:**
```json
{
  "job_id": "abc-123",
  "project": "myproject",
  "name": "my-scrape",
  "schedule": "0 8 * * MON-FRI"
}
```

### GET /jobs

List jobs, optionally filtered by project.

```bash
curl -s "http://127.0.0.1:8420/jobs?project=myproject"
```

**200:**
```json
[
  {
    "job_id": "abc-123",
    "project": "myproject",
    "name": "my-scrape",
    "status": "complete",
    "created_at": "2026-03-16T08:00:00Z",
    "updated_at": "2026-03-16T08:01:30Z",
    "schedule_cron": "0 8 * * MON-FRI",
    "last_run_at": "2026-03-16T08:01:30Z",
    "run_count": 5
  }
]
```

### GET /jobs/{job_id}

Get a single job's detail.

```bash
curl -s "http://127.0.0.1:8420/jobs/abc-123"
```

Returns same shape as a single item from `GET /jobs`.

### DELETE /jobs/{job_id}

Remove a scheduled job. Optionally delete its results.

```bash
curl -s -X DELETE "http://127.0.0.1:8420/jobs/abc-123"
curl -s -X DELETE "http://127.0.0.1:8420/jobs/abc-123?delete_results=true"
```

**204:** No content.

### GET /results/{job_id}

Retrieve results for a job.

```bash
# Latest results
curl -s "http://127.0.0.1:8420/results/abc-123?latest=true"

# Specific run
curl -s "http://127.0.0.1:8420/results/abc-123?run_id=run-456"
```

**200 (results ready):**
```json
{
  "job_id": "abc-123",
  "status": "complete",
  "results": { ... }
}
```

**202 (still running):**
```json
{
  "job_id": "abc-123",
  "status": "running",
  "poll_url": "/jobs/abc-123"
}
```

**404:** Job or results not found.

### GET /errors

Query error logs.

```bash
curl -s "http://127.0.0.1:8420/errors?project=myproject"
curl -s "http://127.0.0.1:8420/errors?job_id=abc-123"
```

**200:**
```json
[
  {
    "job_id": "abc-123",
    "project": "myproject",
    "target_url": "https://example.com",
    "attempt": 2,
    "timestamp": "2026-03-16T08:01:00Z",
    "error_type": "http_error",
    "http_status": 403,
    "fetcher_used": "stealthy",
    "selectors_matched": ["title"],
    "action_taken": "retry",
    "resolved": false
  }
]
```

### GET /health

```bash
curl -s http://127.0.0.1:8420/health
```

**200:**
```json
{
  "status": "ok",
  "uptime_seconds": 3600.0,
  "workers": {
    "max_concurrent": 4,
    "active_tasks": 1,
    "max_browsers": 2,
    "active_browsers": 0
  },
  "projects": {
    "myproject": {
      "job_count": 3,
      "status": "healthy",
      "status_counts": {
        "queued": 0,
        "running": 0,
        "complete": 3,
        "partial": 0,
        "failed": 0
      }
    }
  }
}
```

---

## Webhooks

When a job completes, Scrapeyard POSTs a JSON payload to the URL specified in the config's `webhook.url`. Dispatch is fire-and-forget — failures are logged but don't affect job status.

### Config
```yaml
webhook:
  url: https://myservice.com/ingest    # required
  on: [complete, partial]              # which statuses trigger a webhook
  headers:                             # optional custom headers
    Authorization: "Bearer token123"
  timeout: 10                          # request timeout in seconds
```

### Webhook payload

Scrapeyard POSTs this JSON body to your webhook URL:

```json
{
  "event": "job.complete",
  "job_id": "abc-123",
  "project": "myproject",
  "name": "my-scrape",
  "status": "complete",
  "run_id": "run-456",
  "result_path": "/data/results/myproject/my-scrape/run-456",
  "results_url": "/results/abc-123?run_id=run-456",
  "result_count": 47,
  "error_count": 0,
  "started_at": "2026-03-16T08:00:00Z",
  "completed_at": "2026-03-16T08:01:30Z"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `event` | string | `job.complete`, `job.partial`, or `job.failed` |
| `job_id` | string | Job identifier |
| `project` | string | Project namespace |
| `name` | string | Job name |
| `status` | string | Final job status |
| `run_id` | string or null | Run identifier (use to fetch results) |
| `result_path` | string or null | Filesystem path to results |
| `results_url` | string or null | API path to fetch results |
| `result_count` | int or null | Number of scraped records |
| `error_count` | int | Number of errors during the run |
| `started_at` | string | ISO 8601 timestamp |
| `completed_at` | string | ISO 8601 timestamp |

### Consuming webhooks

Your service should:
1. Accept POST with JSON body at your webhook URL
2. Use `results_url` to fetch full results: `GET http://scrapeyard:8420{results_url}`
3. Return any 2xx to acknowledge receipt (Scrapeyard does not retry on failure)

### Webhook behavior
- Only fires on async jobs (worker pool path). Sync `POST /scrape` does not trigger webhooks.
- The `on` list controls which statuses fire. Default: `[complete, partial]`.
- Dispatch is non-blocking — it never delays job status updates.
- Custom `headers` are sent with the POST (useful for auth tokens).

---

## Environment Variables

All settings use the `SCRAPEYARD_` prefix.

| Variable | Default | Description |
|----------|---------|-------------|
| `WORKERS_MAX_CONCURRENT` | `4` | Total simultaneous scrape tasks |
| `WORKERS_MAX_BROWSERS` | `2` | Max stealthy/dynamic fetcher instances |
| `WORKERS_MEMORY_LIMIT_MB` | `4096` | Reject new work above this threshold |
| `SCHEDULER_JITTER_MAX_SECONDS` | `120` | Random jitter on cron triggers |
| `STORAGE_RESULTS_DIR` | `/data/results` | Result file directory |
| `STORAGE_RETENTION_DAYS` | `30` | Auto-cleanup after N days |
| `STORAGE_MAX_RESULTS_PER_JOB` | `100` | Max retained runs per job |
| `DB_DIR` | `/data/db` | SQLite database directory |
| `ADAPTIVE_DIR` | `/data/adaptive` | Scrapling fingerprint DB |
| `LOG_DIR` | `/data/logs` | Log file directory |
| `CIRCUIT_BREAKER_MAX_FAILURES` | `3` | Consecutive failures before circuit trips |
| `CIRCUIT_BREAKER_COOLDOWN_SECONDS` | `300` | Cooldown before retrying tripped domain |

Example: `SCRAPEYARD_DB_DIR=/tmp/scrapeyard/db`

---

## Common Errors

| Error | Cause | Fix |
|-------|-------|-----|
| `415 Content-Type must be application/x-yaml` | Missing header | Add `-H 'Content-Type: application/x-yaml'` |
| `404 No results found` | Job still running or doesn't exist | Check `GET /jobs/{job_id}` for status |
| `503 Service unavailable` | Worker pool at capacity or memory limit | Retry later or reduce load |
