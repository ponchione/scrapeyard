# Scrapeyard — SPEC.md

> **Version:** 1.1-draft
> **Date:** 2026-03-18
> **Status:** Design Locked — Implementation Guide

---

## 1. Overview

**Scrapeyard** is a containerized, config-driven web scraping microservice built on top of [Scrapling](https://github.com/D4Vinci/Scrapling). It provides a REST API that accepts declarative YAML configurations describing what to scrape, how to scrape it, and what format to return — decoupling scraping logic from the projects that consume it.

The service is designed to be project-agnostic. Any project (job-scout, a product database, a price tracker) communicates with the same service through the same contract: a YAML config payload. The service handles fetching, parsing, error recovery, scheduling, and result storage.

### 1.1 Design Principles

- **Config as contract.** If you can describe the scrape in YAML, the worker handles execution.
- **Project isolation.** Multiple projects use the service simultaneously without interference.
- **Resilience by default.** Scraping is inherently flaky; the service handles retries, validation, and circuit-breaking without per-project boilerplate.
- **Cloud-ready, local-first.** Runs on a single machine in Docker today, but core abstractions (storage, workers, queue) are designed for future cloud migration.

### 1.2 Known Consumer Projects

| Project | Description | Scraping Profile |
|---|---|---|
| eyebox | Optics price comparison pipeline | High volume, hundreds of pages, regular scheduled refreshes, webhook-driven |
| (future projects) | TBD | Varying |

---

## 2. Architecture

### 2.1 High-Level Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Scrapeyard (Docker Container)                               │
│                                                              │
│  FastAPI (Uvicorn)                                           │
│    ├── POST /scrape         (on-demand)                      │
│    ├── POST /jobs           (register scheduled job)         │
│    ├── GET  /jobs           (list jobs)                      │
│    ├── GET  /jobs/{id}      (job detail + status)            │
│    ├── DELETE /jobs/{id}    (remove scheduled job)           │
│    ├── GET  /results/{id}   (retrieve results)               │
│    ├── GET  /health         (service + job health)           │
│    └── GET  /errors         (error log query)                │
│            │                                                 │
│            ▼                                                 │
│       Job Queue (arq + Redis broker)                         │
│       (durable, priority-sorted)                             │
│            │                                                 │
│            ▼                                                 │
│       Worker Pool                                            │
│       ├── Configurable concurrency limit                     │
│       ├── Separate browser instance limit                    │
│       ├── Memory ceiling guard                               │
│       └── Stateless — all state in DB/queue                  │
│            │                                                 │
│            ▼                                                 │
│       Scrapling Engine                                       │
│       ├── Fetcher (basic HTTP + TLS impersonation)           │
│       ├── StealthyFetcher (Camoufox, anti-bot bypass)        │
│       ├── DynamicFetcher (Playwright, JS rendering)          │
│       └── Adaptive tracking (auto_save / relocate)           │
│            │                                                 │
│            ▼                                                 │
│       Result Formatter                                       │
│       ├── JSON                                               │
│       ├── Markdown                                           │
│       ├── HTML (raw)                                         │
│       └── JSON + Markdown (combo)                            │
│            │                                                 │
│            ▼                                                 │
│       Storage Layer (abstracted interface)                    │
│       ├── SQLite (job store, result metadata, errors,        │
│       │          adaptive fingerprints)                      │
│       └── Local disk (result files)                          │
│                                                              │
│  APScheduler (cron jobs → enqueues to same job queue)        │
│                                                              │
└──────────────────────────────────────────────────────────────┘
         │
    Docker Volume (persistent)
    ├── /data/db/        (SQLite databases)
    ├── /data/results/   (result files)
    └── /data/adaptive/  (Scrapling fingerprint DB)
```

### 2.2 Request Flow

**On-demand scrape:**

1. Client sends `POST /scrape` with YAML config (inline or file reference).
2. FastAPI validates the config against the schema.
3. Job is persisted and enqueued to the arq queue via Redis with assigned priority.
4. Worker picks up the job, executes via Scrapling, formats results, and writes to storage.
5. If the request is in sync-wait mode, the API waits for queued job completion up to `sync_timeout_seconds`.
6. If the job completes within the wait window, the API returns `200` with results inline.
7. Otherwise, the API returns `202 Accepted` with a `job_id` for polling.
8. Client retrieves results via `GET /results/{job_id}`.

**Scheduled scrape:**

1. Client sends `POST /jobs` with YAML config including a `schedule` block.
2. Config is validated and stored in the job store (SQLite).
3. APScheduler registers a cron trigger for the job.
4. At each trigger time (with jitter applied), the scheduler enqueues the job to the same Redis-backed arq queue.
5. Worker executes identically to an on-demand scrape.
6. Results accumulate in storage under the job's namespace.

### 2.3 Sync/Async Response Strategy

All scrape requests are enqueued first. The API then decides whether to wait for queued completion or return immediately based on response mode and scrape complexity.

In `auto` mode, the API waits for completion only for simple jobs:

| Condition | Response |
|---|---|
| Single target, no pagination, basic fetcher | Wait up to `sync_timeout_seconds`; return `200` if complete, else `202` |
| Multi-target, pagination, or stealthy/dynamic fetcher | `202 Accepted` immediately; poll `GET /results/{job_id}` |

The caller can override this behavior explicitly:

```yaml
execution:
  mode: async   # enqueue and return 202 immediately
  # or
  mode: sync    # enqueue, then wait up to sync_timeout_seconds
```

`sync` never bypasses the worker path. It means "wait on the queued job up to timeout," not "execute inline in the API process."

Recommended service-level setting:

```yaml
api:
  sync_timeout_seconds: 15
```

---

## 3. Configuration Schema

### 3.1 Format

All configs are **YAML only**. Configs are submitted either as the request body of `POST /scrape` or `POST /jobs`, or as file references.

### 3.2 Schema Tiers

The config supports two tiers of complexity. Tier 1 covers ~80% of use cases with flat, declarative syntax. Tier 2 adds optional blocks for transforms, validation overrides, and orchestration control.

### 3.3 Tier 1 — Simple Declarative

```yaml
project: job-scout
name: python-jobs-indeed

target:
  url: "https://indeed.com/jobs?q=python"
  fetcher: stealthy

selectors:
  title:      ".jobTitle::text"
  company:    ".companyName::text"
  location:   ".companyLocation::text"
  link:       ".jobTitle a::attr(href)"

pagination:
  next: ".pagination a[data-testid='next']"
  max_pages: 5

output:
  format: json
```

### 3.4 Tier 2 — Enhanced

```yaml
project: job-scout
name: python-jobs-multi

targets:
  - url: "https://indeed.com/jobs?q=python"
    fetcher: stealthy
    adaptive_domain: indeed.com
    selectors:
      title:      ".jobTitle::text"
      company:    ".companyName::text"
      link:
        query:    ".jobTitle a::attr(href)"
        transform: prepend("https://indeed.com")
    pagination:
      next: ".pagination a[data-testid='next']"
      max_pages: 5

  - url: "https://coolstartup.com/careers"
    fetcher: basic
    selectors:
      title:      "h3.role-title::text"
      department: ".dept-label::text"

adaptive: true   # explicit override (see Section 6)

retry:
  max_attempts: 3
  backoff: exponential
  backoff_max: 30

validation:
  required_fields: [title, company]
  min_results: 1
  on_empty: retry

execution:
  concurrency: 2
  delay_between: 2
  domain_rate_limit: 3
  mode: async
  priority: normal

schedule:
  cron: "0 8 * * MON-FRI"

output:
  format: json+markdown
  group_by: target        # "target" (default) or "merge"
```

### 3.5 Config Field Reference

#### Top-Level Fields

| Field | Type | Required | Description |
|---|---|---|---|
| `project` | string | **Yes** | Project namespace. Isolates jobs, results, and errors. |
| `name` | string | **Yes** | Unique job name within the project namespace. |
| `target` | object | One of `target` or `targets` | Single scrape target definition. |
| `targets` | array | One of `target` or `targets` | Multiple scrape target definitions. |
| `adaptive` | boolean | No | Override adaptive tracking. Default: auto (see Section 6). |
| `retry` | object | No | Retry policy. Defaults applied if omitted. |
| `validation` | object | No | Result validation rules. |
| `execution` | object | No | Concurrency and orchestration settings. |
| `schedule` | object | No | Cron-style scheduling. Omit for on-demand only. |
| `output` | object | No | Output format and grouping. Defaults to `json`, grouped by target. |

#### Target Fields

| Field | Type | Required | Description |
|---|---|---|---|
| `url` | string | **Yes** | Target URL to scrape. |
| `fetcher` | string | No | `basic` (default), `stealthy`, or `dynamic`. |
| `adaptive_domain` | string | No | Override the adaptive fingerprint namespace for this target. Defaults to the normalized hostname. |
| `selectors` | object | **Yes** | Named selector definitions (see below). |
| `pagination` | object | No | Pagination rules (see below). |

#### Selector Syntax

Selectors support two forms:

**Short form (CSS, default):**
```yaml
selectors:
  title: ".jobTitle::text"
```

**Long form (explicit type, optional transform):**
```yaml
selectors:
  title:
    query: "//h2[@class='title']/text()"
    type: xpath
    transform: trim
  link:
    query: ".job-link::attr(href)"
    transform: prepend("https://example.com")
```

Supported selector types: `css` (default), `xpath`.

#### Transform Vocabulary

Transforms are a fixed set of safe, predictable string operations. No arbitrary code execution.

| Transform | Description | Example |
|---|---|---|
| `trim` | Strip leading/trailing whitespace | `trim` |
| `prepend(str)` | Prepend a string | `prepend("https://example.com")` |
| `append(str)` | Append a string | `append("/details")` |
| `replace(old, new)` | String replacement | `replace("$", "")` |
| `regex(pattern, group)` | Regex extraction | `regex("\\d+", 0)` |
| `lowercase` | Convert to lowercase | `lowercase` |
| `uppercase` | Convert to uppercase | `uppercase` |
| `join(sep)` | Join multiple results | `join(", ")` |

#### Pagination Fields

| Field | Type | Required | Description |
|---|---|---|---|
| `next` | string | **Yes** | CSS/XPath selector for the "next page" element. |
| `max_pages` | integer | No | Maximum pages to scrape. Default: 10. |

#### Retry Fields

| Field | Type | Default | Description |
|---|---|---|---|
| `max_attempts` | integer | 3 | Maximum retry attempts per request. |
| `backoff` | string | `exponential` | Backoff strategy: `exponential`, `linear`, `fixed`. |
| `backoff_max` | integer | 30 | Maximum backoff delay in seconds. |
| `retryable_status` | array | `[429, 500, 502, 503, 504]` | HTTP status codes that trigger a retry. |

#### Validation Fields

| Field | Type | Default | Description |
|---|---|---|---|
| `required_fields` | array | `[]` | Fields that must be non-empty for a result to be valid. |
| `min_results` | integer | 0 | Minimum number of results expected. 0 = no minimum. |
| `on_empty` | string | `warn` | Action when selectors return empty: `retry`, `warn`, `fail`, `skip`. |

#### Execution Fields

| Field | Type | Default | Description |
|---|---|---|---|
| `concurrency` | integer | 2 | Max targets scraped simultaneously within this job. |
| `delay_between` | integer | 2 | Seconds between starting concurrent targets. |
| `domain_rate_limit` | integer | 3 | Minimum seconds between requests to the same domain. |
| `mode` | string | `auto` | Response mode: `auto`, `sync`, `async`. `sync` means wait on queued completion up to timeout. |
| `priority` | string | `normal` | Queue priority: `high`, `normal`, `low`. |

#### Schedule Fields

| Field | Type | Required | Description |
|---|---|---|---|
| `cron` | string | **Yes** | Cron expression (e.g., `"0 8 * * MON-FRI"`). |
| `enabled` | boolean | No | Default `true`. Set `false` to pause without deleting. |

#### Output Fields

| Field | Type | Default | Description |
|---|---|---|---|
| `format` | string | `json` | Output format: `json`, `markdown`, `html`, `json+markdown`. |
| `group_by` | string | `target` | Result grouping: `target` (grouped) or `merge` (flat with source field). |

### 3.6 Submission Validation Policy

Config validation at submit time is static only.

- `POST /scrape` and `POST /jobs` validate YAML structure, required fields, enum values, and cheap semantic constraints.
- Submit-time validation does **not** fetch target URLs or test selectors against live pages.
- Selector effectiveness is evaluated at runtime through the existing `validation` block and normal scrape execution.
- If deeper preflight analysis is ever added, it should live in a separate dry-run or lint endpoint, not in the main submission path.

---

## 4. API Specification

### 4.1 Endpoints

#### `POST /scrape`

Submit an on-demand scrape job.

- **Request body:** YAML config (Content-Type: `application/x-yaml`)
- **Behavior:** Create a job, enqueue it, then either wait for queued completion or return immediately depending on `execution.mode`
- **Response (completed within wait window):** `200 OK` with results
- **Response (still running or async mode):** `202 Accepted` with `{ "job_id": "...", "status": "queued", "poll_url": "/results/{job_id}" }`
- **Response (invalid config):** `422 Unprocessable Entity`

#### `POST /jobs`

Register a scheduled or reusable job.

- **Request body:** YAML config with `schedule` block
- **Response:** `201 Created` with `{ "job_id": "...", "project": "...", "name": "...", "schedule": "..." }`
- **Response (invalid config):** `422 Unprocessable Entity`

#### `GET /jobs`

List all registered jobs. Filterable by project.

- **Query params:** `?project=job-scout`
- **Response:** `200 OK` with array of job summaries

#### `GET /jobs/{job_id}`

Get a specific job's config, status, and run history.

- **Response:** `200 OK` with job detail including last run status, next scheduled run, and run count

#### `DELETE /jobs/{job_id}`

Remove a scheduled job and optionally its results.

- **Query params:** `?delete_results=true` (default `false`)
- **Response:** `204 No Content`

#### `GET /results/{job_id}`

Retrieve results for a job.

- **Query params:** `?latest=true` (default), `?run_id=...` for historical, `?project=...` for filtering
- **Response:** `200 OK` with results in the configured output format
- **Response (not ready):** `202 Accepted` with `{ "status": "running" }`
- **Response (not found):** `404 Not Found`

#### `GET /health`

Service health and per-job status overview.

- **Response:** `200 OK` with service uptime, worker pool status, and per-project job health summary (healthy, degraded, failing)

#### `GET /errors`

Query error logs.

- **Query params:** `?project=...`, `?job_id=...`, `?since=...`, `?error_type=...`
- **Response:** `200 OK` with array of structured error records

---

## 5. Error Handling & Resilience

### 5.1 Three-Layer Model

Error handling operates at three levels, each with distinct responsibilities.

#### Layer 1: Per-Request Retry

Handles transient network and HTTP failures at the individual request level.

- Exponential backoff by default (1s → 2s → 4s), capped at `backoff_max`.
- Only retries on status codes in `retryable_status` list.
- Configurable per-job via the `retry` config block.
- Default: 3 attempts with exponential backoff, max 30s delay.

#### Layer 2: Per-Target Result Validation

Detects silent failures — the page returned 200 but selectors matched nothing useful.

- Validates against `required_fields` and `min_results` after each target completes.
- Action on failure determined by `on_empty`: retry the target, log a warning, fail the target, or skip it.
- Catches the scenario where a site serves a bot-detection page or completely different content.

#### Layer 3: Job-Level Circuit Breaker

Prevents runaway failures from consuming resources or hammering unresponsive sites.

- Tracks consecutive failures per target domain within a job.
- After `max_consecutive_failures` (default: 3), trips the circuit breaker for that domain.
- Cooldown period (default: 300s) before retrying.
- Job-level `fail_strategy` determines overall behavior:
  - `partial` — return results from successful targets, report failures.
  - `all_or_nothing` — fail the entire job if any target fails.
  - `continue` — keep going regardless, log all failures.
- Default: `partial`.

### 5.2 Structured Error Records

Every failure is captured as a structured record in SQLite:

```json
{
  "job_id": "senior-python-jobs",
  "project": "job-scout",
  "target_url": "https://example.com/jobs",
  "attempt": 2,
  "timestamp": "2026-03-04T14:30:00Z",
  "error_type": "content_empty | http_error | network_error | browser_error | timeout",
  "http_status": 200,
  "fetcher_used": "stealthy",
  "selectors_matched": {"title": 0, "company": 0, "link": 0},
  "action_taken": "retry | warn | fail | skip | circuit_break",
  "resolved": false
}
```

### 5.3 Logging

- **CLI output:** Structured log lines to stdout/stderr (suitable for `docker logs`).
- **Log files:** Rotating log files in the persistent volume for historical review.
- **DB status:** Job run status (success, partial, failed) stored per run in SQLite.
- **Notable events:** Adaptive relocations, circuit breaker trips, fetcher escalations logged at INFO level.

---

## 6. Adaptive Element Tracking

### 6.1 Strategy

Adaptive tracking is governed by the following default logic:

| Job Type | Adaptive Default | Rationale |
|---|---|---|
| On-demand (`POST /scrape`) | **Off** | One-shot scrapes don't benefit from fingerprinting. |
| Scheduled (`schedule` block present) | **On** | Repeated scrapes benefit from self-healing selectors. |

The config can always override explicitly:

```yaml
adaptive: true    # force on
adaptive: false   # force off
```

### 6.2 Behavior

When adaptive is enabled:

1. **First run:** Elements matched by selectors are fingerprinted via Scrapling's `auto_save=True`. Fingerprints include tag name, text content, attributes (names and values), sibling tags, parent context, and DOM path.
2. **Subsequent runs:** If a selector fails to match or returns unexpected results, Scrapling activates `adaptive=True` and searches the page for elements matching the stored fingerprint using similarity scoring.
3. **Relocation logging:** When an element is relocated via adaptive matching, a notable event is logged:

```
[INFO] Adaptive relocation: project=job-scout job=python-jobs-indeed
       selector=".jobTitle" → relocated (score: 0.87)
       domain=indeed.com timestamp=2026-03-05T08:01:23Z
```

### 6.3 Storage

Adaptive fingerprints are stored in Scrapling's internal SQLite database, persisted via Docker volume at `/data/adaptive/`. Each project's fingerprints are namespaced by the `project` + `adaptive_domain`, where `adaptive_domain` defaults to the normalized hostname and can be overridden explicitly per target.

---

## 7. Scaling & Multi-Project Support

### 7.1 Job Queue + Worker Pool

The service does **not** execute scrapes directly from the API layer. All scrape requests — both on-demand and scheduled — are enqueued to an arq queue backed by Redis and executed by a worker pool.

**Queue:** arq with Redis persistence. Jobs are priority-sorted so on-demand requests aren't blocked by large background crawls, and queued work survives Scrapeyard process restarts as long as Redis persistence is intact.

**Worker pool configuration (service-level):**

```yaml
workers:
  max_concurrent: 4            # total simultaneous scrape tasks
  max_browsers: 2              # of those, max using stealthy/dynamic fetchers
  memory_limit_mb: 4096        # reject new work if exceeded
```

Priority levels: `high`, `normal` (default), `low`.

### 7.2 Project Namespacing

Every config requires a `project` field. This provides complete isolation:

- **Jobs:** `GET /jobs?project=job-scout` returns only job-scout jobs.
- **Results:** `GET /results/{id}` scoped to the job's project.
- **Errors:** `GET /errors?project=job-scout` returns only job-scout errors.
- **Storage:** Result files organized as `/data/results/{project}/{job_name}/{run_id}/`.

### 7.3 Rate Limiting

Rate limits are **per-project, per-domain**. Each project controls its own throttling via the `execution.domain_rate_limit` config field.

**Accepted tradeoff:** If two projects scrape the same domain simultaneously, the service does not coordinate between them. This is managed operationally through schedule staggering, not infrastructure. This keeps the implementation simpler and gives each project full autonomy.

### 7.4 Schedule Jitter

To prevent thundering herd when multiple projects schedule jobs at the same time, the scheduler applies automatic random jitter:

```yaml
# Service-level config
scheduler:
  jitter_max_seconds: 120     # jobs start within a random 0-120s window
```

A job scheduled for "0 8 * * MON-FRI" will fire between 8:00:00 and 8:02:00.

---

## 8. Result Delivery

### 8.1 Output Formats

| Format | Content-Type | Description |
|---|---|---|
| `json` | `application/json` | Structured data, ideal for programmatic consumption and LLM pipelines. |
| `markdown` | `text/markdown` | Human-readable tables/lists, also effective for LLM context windows. |
| `html` | `text/html` | Raw page content for debugging or re-parsing with different selectors. |
| `json+markdown` | Both files produced | JSON as canonical data store, Markdown as LLM-friendly rendition. |

### 8.2 Result Grouping

For multi-target jobs, results are **grouped by target** by default:

```json
{
  "job_id": "python-jobs-multi",
  "project": "job-scout",
  "status": "complete",
  "completed_at": "2026-03-05T08:02:15Z",
  "results": {
    "indeed.com": {
      "status": "success",
      "count": 47,
      "data": [{"title": "...", "company": "..."}]
    },
    "coolstartup.com": {
      "status": "success",
      "count": 3,
      "data": [{"title": "...", "department": "..."}]
    }
  },
  "errors": []
}
```

With `output.group_by: merge`, results are flattened into a single array with a `_source` field injected:

```json
{
  "results": [
    {"title": "...", "company": "...", "_source": "indeed.com"},
    {"title": "...", "department": "...", "_source": "coolstartup.com"}
  ]
}
```

### 8.3 Result Storage & Retention

- **Result files** are stored on disk at `/data/results/{project}/{job_name}/{run_id}/`.
- **Result metadata** (job ID, timestamp, status, record count, file path) is stored in SQLite.
- **Retention policy** (service-level defaults):

```yaml
storage:
  results_dir: /data/results
  retention_days: 30
  max_results_per_job: 100
```

Auto-cleanup runs periodically, removing results older than `retention_days` and keeping only the latest `max_results_per_job` runs per scheduled job.

---

## 9. Cloud-Readiness (v1 Requirements)

These patterns are implemented in v1 to ensure future cloud migration is a configuration change, not a rewrite.

### 9.1 Storage Abstraction

All storage access goes through defined interfaces (Python `Protocol` classes):

```python
class JobStore(Protocol):
    async def save_job(self, job: Job) -> str: ...
    async def get_job(self, job_id: str) -> Job: ...
    async def list_jobs(self, project: str) -> list[Job]: ...
    async def delete_job(self, job_id: str) -> None: ...

class ResultStore(Protocol):
    async def save_result(self, job_id: str, data: Any, format: str) -> str: ...
    async def get_result(self, job_id: str, run_id: str | None) -> Any: ...

class ErrorStore(Protocol):
    async def log_error(self, error: ErrorRecord) -> None: ...
    async def query_errors(self, filters: ErrorFilters) -> list[ErrorRecord]: ...
```

**v1 implementations:** `SQLiteJobStore`, `LocalResultStore`, `SQLiteErrorStore`.

**Future swap targets:** Postgres, S3, managed queue services — implemented as alternate classes behind the same interfaces.

### 9.2 Stateless Workers

Workers hold no durable in-memory state between jobs. All durable state (job configs, queue state, results, adaptive fingerprints, error logs) lives in Redis or the storage layer.

Job delivery is **at-least-once**. A worker that crashes mid-scrape may result in the job being retried after recovery. Job handlers therefore must be idempotent with respect to `job_id` and `run_id`, or otherwise tolerate duplicate execution safely.

This means the service can scale horizontally in the future — multiple identical worker containers pulling from a shared queue — without architectural changes.

---

## 10. Infrastructure

### 10.1 Tech Stack

| Component | Technology |
|---|---|
| API framework | FastAPI + Uvicorn |
| Scraping engine | Scrapling (with fetchers) |
| Job queue | arq + Redis |
| Scheduler | APScheduler |
| Database | SQLite |
| Dependency management | Poetry |
| Containerization | Docker + Docker Compose |
| Python version | 3.10+ |

### 10.2 Docker Compose Structure

```yaml
services:
  redis:
    image: redis:7-alpine
    command: ["redis-server", "--appendonly", "yes"]
    volumes:
      - redis-data:/data

  scrapeyard:
    build: .
    ports:
      - "8420:8420"
    depends_on:
      - redis
    volumes:
      - scrapeyard-data:/data
    environment:
      - ARQ_REDIS_SETTINGS=redis://redis:6379/0
      - SCRAPEYARD_WORKERS_MAX_CONCURRENT=4
      - SCRAPEYARD_WORKERS_MAX_BROWSERS=2
      - SCRAPEYARD_WORKERS_MEMORY_LIMIT_MB=4096
      - SCRAPEYARD_SYNC_TIMEOUT_SECONDS=15
      - SCRAPEYARD_SCHEDULER_JITTER_MAX_SECONDS=120
      - SCRAPEYARD_STORAGE_RETENTION_DAYS=30

volumes:
  redis-data:
  scrapeyard-data:
```

### 10.3 Persistent Volume Layout

```
/data/
├── db/
│   ├── jobs.db              # Job configs, run history, metadata
│   ├── errors.db            # Structured error records
│   └── results_meta.db      # Result metadata index
├── results/
│   └── {project}/
│       └── {job_name}/
│           └── {run_id}/
│               ├── results.json
│               └── results.md    # if json+markdown
├── adaptive/
│   └── scrapling.db          # Scrapling's adaptive fingerprint store
└── logs/
    └── scrapeyard.log        # Rotating log files
```

---

## 11. Future Work (v2+)

The following features are explicitly deferred and not part of v1 implementation.

| Feature | Description | Trigger for inclusion |
|---|---|---|
| **Follow / chained scrapes** | Scrape a listing page, then follow each link to detail pages. `follow` block in config. | Needed when job-scout requires scraping individual job detail pages. |
| ~~**Webhooks**~~ | ~~Optional callback URL notified on job completion.~~ | **Implemented** — `WebhookConfig` on `ScrapeConfig`, fire-and-forget dispatch via `src/scrapeyard/webhook/`. |
| **Cloud deployment** | Postgres, S3, managed queues, auth, secrets management. | Needed when the service must run 24/7 or serve remote clients. |
| **Global cross-project rate limiting** | Shared per-domain rate limits across all projects. | Needed when many projects frequently hit the same domains simultaneously. |
| **Fetcher escalation** | Auto-escalate fetcher tier (basic → stealthy → dynamic) on 403/block detection. | Useful but adds complexity; evaluate after v1 usage patterns emerge. |
| **Config templates** | Reusable partial configs (e.g., shared retry policy, common selector patterns). | Useful once 10+ configs exist with repeated boilerplate. |
| **Result diffing** | Compare results between runs to detect meaningful changes. | Valuable for price tracking and product database freshness monitoring. |
| **UI dashboard** | Web UI for viewing jobs, results, errors, and adaptive tracking status. | Quality-of-life improvement once the service is mature. |

---

## 12. Resolved Design Decisions

The following decisions were locked on 2026-03-18 to guide implementation:

1. **Queue persistence:** v1 uses Redis-backed `arq`, not an in-memory queue. Queue durability is part of the core architecture.
2. **Execution path:** all jobs are enqueued first. The API never runs scrape jobs inline.
3. **Sync semantics:** `execution.mode: sync` means "wait on queued completion up to `sync_timeout_seconds`," then fall back to `202 Accepted` if unfinished.
4. **Auto response heuristic:** in `auto` mode, wait only for single-target, non-paginated, `basic` fetcher jobs. All other jobs return `202` immediately after enqueue.
5. **Config validation depth:** submission-time validation is static only; no live fetches or selector probing during `POST /scrape` or `POST /jobs`.
6. **Adaptive domain handling:** adaptive fingerprints are scoped by `project + adaptive_domain`, where `adaptive_domain` defaults to the normalized hostname and can be overridden explicitly per target.
