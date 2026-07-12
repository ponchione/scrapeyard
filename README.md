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
printf -v SCRAPEYARD_API_CREDENTIALS \
  '{"local-admin":{"identity":"local-admin","secret":"%s","scopes":["submit","read","schedule-admin","delete","health-detail"]}}' \
  "$SCRAPEYARD_API_KEY"
export SCRAPEYARD_API_CREDENTIALS
export SCRAPEYARD_ENCRYPTION_ACTIVE_KEY_ID=local-v1
export SCRAPEYARD_ENCRYPTION_KEYS="{\"local-v1\":\"$(openssl rand -base64 32 | tr -d '\n')\"}"
export SCRAPEYARD_DB_DIR=/tmp/scrapeyard/db
export SCRAPEYARD_STORAGE_RESULTS_DIR=/tmp/scrapeyard/results
export SCRAPEYARD_ADAPTIVE_DIR=/tmp/scrapeyard/adaptive
export SCRAPEYARD_LOG_DIR=/tmp/scrapeyard/logs
poetry run uvicorn scrapeyard.main:app --host 0.0.0.0 --port 8420 --workers 1
```

Check health:

```bash
curl -s http://127.0.0.1:8420/health
```

## Docker

Start the full local stack:

```bash
export SCRAPEYARD_API_KEY="$(openssl rand -hex 32)"
printf -v SCRAPEYARD_API_CREDENTIALS \
  '{"local-admin":{"identity":"local-admin","secret":"%s","scopes":["submit","read","schedule-admin","delete","health-detail"]}}' \
  "$SCRAPEYARD_API_KEY"
export SCRAPEYARD_API_CREDENTIALS
export SCRAPEYARD_ENCRYPTION_ACTIVE_KEY_ID=local-v1
export SCRAPEYARD_ENCRYPTION_KEYS="{\"local-v1\":\"$(openssl rand -base64 32 | tr -d '\n')\"}"
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build
```

Stop it:

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml down
```

The Compose setup starts Scrapeyard and Redis, mounts persistent data at
`/data`, and expects `SCRAPEYARD_API_CREDENTIALS` from the shell or a local `.env`
file.

The production base Compose file exposes no host port. The explicit local
override publishes `127.0.0.1:8420` by default. Set
`SCRAPEYARD_BIND_ADDRESS=0.0.0.0` only on a trusted, firewalled development
host when peer containers must use the host gateway; a shared private Docker
network is preferred.

Load the repository's restrictive Chromium AppArmor profile once per host
before starting Compose (and remove it during decommissioning):

```bash
sudo security/install-chromium-apparmor-profile.sh install
```

When browser-runtime dependencies change, rebuild the app container:

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml \
  up -d --build --force-recreate scrapeyard
```

The image installs Playwright Chromium for standard `fetcher: dynamic` jobs,
rebrowser Chromium for `fetcher: dynamic` with `browser.stealth: true`, and
Camoufox assets for `fetcher: stealthy`. Browser versions, revisions, base
images, and the Camoufox archive checksum are pinned. The image starts directly
as the non-root UID/GID 10001 with a Chromium-specific seccomp/AppArmor policy
and no-new-privileges. Chromium uses unprivileged user namespaces; there is no
setuid helper or startup root repair. The root filesystem is read-only, the
process has no effective capabilities, and only `SYS_CHROOT` remains in the
bounding set for Chromium's namespace sandbox. Only `/data` plus bounded tmpfs
mounts are writable. Existing volumes need the one-time ownership migration in
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## API

The typed v1 response/error contract, result compatibility mode, versioning
policy, and pagination headers are documented in
[docs/API.md](docs/API.md). Every response advertises
`X-Scrapeyard-API-Version: 1`.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health`, `/health/live` | Minimal public process liveness |
| `GET` | `/health/ready` | Protected dependency/capacity readiness |
| `GET` | `/metrics` | Protected Prometheus operational metrics |
| `POST` | `/scrape` | Submit an ad hoc scrape |
| `POST` | `/jobs` | Register a scheduled job |
| `PUT` | `/jobs/{job_id}` | Replace future-run config and schedule |
| `GET` | `/jobs` | List jobs |
| `GET` | `/jobs/{job_id}` | Read job details and run state |
| `POST` | `/jobs/{job_id}/pause` | Pause future cron fires |
| `POST` | `/jobs/{job_id}/resume` | Resume future cron fires |
| `POST` | `/jobs/{job_id}/trigger` | Queue a manual run using the current config version |
| `POST` | `/jobs/{job_id}/cancel` | Cancel the current queued/running delivery and wait for quiescence |
| `DELETE` | `/jobs/{job_id}` | Delete a job |
| `GET` | `/results/{job_id}` | Read stored results |
| `GET` | `/errors` | Query stored errors |

Protected endpoints require `X-API-Key` when `SCRAPEYARD_API_CREDENTIALS` is
set. Named credentials declare `submit`, `read`, `schedule-admin`, `delete`,
and/or `health-detail` scopes and may be restricted to project names.
`POST /scrape` accepts an optional `Idempotency-Key` containing 1–128 visible
ASCII bytes. For 24 hours by default, retrying byte-identical YAML with the
same authenticated caller and key returns the original `job_id` and `run_id`
without another enqueue. Async submissions and sync requests that are still
active return `202`; a completed sync submission returns `200`. Replays include
`Idempotency-Replayed: true`. Reusing a live key with different YAML returns
`409`. When authentication is disabled, all requests share one explicit
`local-development` idempotency scope, so keys must be unique across all local
clients. Omit the header to retain the original create-on-every-request
behavior. Only SHA-256 key/API-key digests are persisted; plaintext keys are
not logged.

Cancellation and deletion are separate, idempotent state transitions. Active
jobs must be cancelled before deletion; preserved results remain readable after
the job/YAML is removed. See
[docs/JOB_LIFECYCLE.md](docs/JOB_LIFECYCLE.md) for exact response codes, Redis
failure behavior, retention policy, and crash-retry boundaries.

### Queue priority

`execution.priority` is real queue ordering, not a timing hint. With the
default `SCRAPEYARD_QUEUE_NAME=scrapeyard`, accepted deliveries wait in
`scrapeyard:priority:high`, `scrapeyard:priority:normal`, or
`scrapeyard:priority:low`. One embedded arq Worker executes admitted work from
the existing `scrapeyard` queue, so `SCRAPEYARD_WORKERS_MAX_CONCURRENT` remains
a global handler limit and browser permits remain process-wide.

Admission is work-conserving weighted round robin with the repeating fair-turn
cycle `high, high, high, high, normal, normal, low`. The current fair turn is
preferred; when it is empty, the highest non-empty priority runs. With all
three queues continuously backlogged, normal receives two of every seven
admissions and low receives one. A waiting lower-priority delivery is selected
within at most seven further admission decisions (at most six other starts),
after work already admitted or running. There is no wall-clock bound because
running jobs are never preempted. Deliveries are FIFO within one priority.

Ad-hoc, scheduled, and startup-recovered deliveries all use this routing. The
first enqueue of a `run_id` wins across all queues; a duplicate cannot change
its priority or create another executable delivery. `/health/ready` reports
`workers.queue_depths` for the three priority intake queues. These counts
include waiting and deferred sorted-set members, but exclude work already
admitted to the base queue and work in progress.

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
  -H "Idempotency-Key: $(openssl rand -hex 16)" \
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
For `fetcher: stealthy`, `browser.additional_arguments` deliberately accepts
only YAML-safe Camoufox overrides: `locale` (one tag or a list), `fonts` (a
bounded list of names), `custom_fonts_only` (which requires `fonts`), and
`window` (a two-integer `[width, height]` list). Python-object controls such as
custom fingerprints and screen constraint objects are not accepted by the
service schema.

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

Terminal webhook intent is durable before a run is reported complete. Each
logical event uses a stable `whv1_<sha256>` delivery ID derived only from its
job ID, run ID, and event, and the same ID is sent to the receiver for
deduplication. HTTP delivery remains asynchronous and at-least-once; startup
repairs a missing terminal intent and replays pending rows without resetting
already delivered or permanently failed deliveries. See
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for the transaction and cross-database
consistency boundaries.

The six-hour cleanup pass applies normal result retention first, then validates
metadata-backed artifacts and reconciles stale filesystem orphans and known
atomic-write temporary files. Reconciliation is dry-run by default. A
metadata row remains authoritative even after its parent job is deleted with
`delete_results=false`; missing, unsafe, unreadable, or corrupt `results.json`
files are reported as storage failures and are not repaired or deleted. See
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#result-artifact-reconciliation) before
enabling destructive reconciliation.

## Settings

All service settings use the `SCRAPEYARD_` prefix. The most commonly changed
settings are:

| Variable | Default | Purpose |
| --- | --- | --- |
| `SCRAPEYARD_API_CREDENTIALS` | empty | Named JSON credentials with identity, secret, scopes, and optional projects |
| `SCRAPEYARD_API_KEYS` | empty | Deprecated full-admin migration allow-list; remove after converting to named credentials |
| `SCRAPEYARD_ENCRYPTION_KEYS` | empty | JSON key-ID to base64 32-byte AES key map for persisted secrets |
| `SCRAPEYARD_ENCRYPTION_ACTIVE_KEY_ID` | empty | Key ID used for new writes and startup rotation |
| `SCRAPEYARD_HEALTH_PROBE_TIMEOUT_SECONDS` | `2` | Per-operation timeout for detailed readiness probes and metric snapshots |
| `SCRAPEYARD_METRICS_REFRESH_INTERVAL_SECONDS` | `5` | Minimum interval between durable Prometheus gauge refreshes |
| `SCRAPEYARD_REDIS_DSN` | `redis://redis:6379/0` | Redis connection for `arq` |
| `SCRAPEYARD_QUEUE_NAME` | `scrapeyard` | Base arq execution queue; priority intake queues append `:priority:high`, `:priority:normal`, and `:priority:low` |
| `SCRAPEYARD_DB_DIR` | `/data/db` | SQLite database directory |
| `SCRAPEYARD_STORAGE_RESULTS_DIR` | `/data/results` | Result artifact directory |
| `SCRAPEYARD_STORAGE_ORPHAN_GRACE_SECONDS` | `86400` | Minimum artifact age before orphan/temp removal eligibility |
| `SCRAPEYARD_STORAGE_RECONCILIATION_DRY_RUN` | `true` | Report eligible orphan/temp removals without changing files |
| `SCRAPEYARD_ADAPTIVE_DIR` | `/data/adaptive` | Scrapling adaptive state directory |
| `SCRAPEYARD_LOG_DIR` | `/data/logs` | Log directory |
| `SCRAPEYARD_SYNC_TIMEOUT_SECONDS` | `15` | Max wait for sync scrape responses |
| `SCRAPEYARD_SYNC_POLL_DELAY_SECONDS` | `0.5` | Sync response polling interval |
| `SCRAPEYARD_IDEMPOTENCY_KEY_MAX_BYTES` | `128` | Maximum visible-ASCII idempotency key length |
| `SCRAPEYARD_IDEMPOTENCY_RETENTION_HOURS` | `24` | Caller/key replay and conflict window |
| `SCRAPEYARD_IDEMPOTENCY_CLEANUP_BATCH_SIZE` | `1000` | Maximum expired key records removed per cleanup pass |
| `SCRAPEYARD_SCHEDULER_MISFIRE_GRACE_SECONDS` | `60` | Maximum lateness for one coalesced in-process cron fire |
| `SCRAPEYARD_WORKERS_MAX_CONCURRENT` | `4` | Max concurrent jobs |
| `SCRAPEYARD_WORKERS_MAX_BROWSERS` | `2` | Max concurrent browser targets across all jobs |
| `SCRAPEYARD_WORKERS_CANCELLATION_GRACE_SECONDS` | `10` | Bounded arq abort and worker-quiescence wait for cancellation |
| `SCRAPEYARD_WORKERS_QUEUED_CLAIM_TIMEOUT_SECONDS` | `300` | Age after which an unclaimed queued delivery may be replaced |
| `SCRAPEYARD_WORKERS_RUNNING_HEARTBEAT_TIMEOUT_SECONDS` | `600` | Time since the last persisted run heartbeat before recovery is allowed |
| `SCRAPEYARD_WORKERS_HEARTBEAT_INTERVAL_SECONDS` | `30` | Monotonic interval between persisted run heartbeats; at most one third of the running timeout |
| `SCRAPEYARD_RUN_MAX_DURATION_SECONDS` | `900` | Overall monotonic deadline for one run |
| `SCRAPEYARD_RUN_MAX_FETCHED_BYTES` | `104857600` | Aggregate reliably measured basic-response bytes |
| `SCRAPEYARD_RUN_MAX_EXTRACTED_RECORDS` | `100000` | Aggregate extracted records across targets, pages, and validation retries |
| `SCRAPEYARD_RUN_MAX_SERIALIZED_RESULT_BYTES` | `52428800` | Exact maximum UTF-8 bytes for persisted result JSON |
| `SCRAPEYARD_RUN_MAX_BROWSER_DEBUG_BYTES` | `26214400` | Aggregate browser excerpt and screenshot bytes per run |
| `SCRAPEYARD_TRANSFORM_REGEX_TIMEOUT_SECONDS` | `0.1` | Per-operation deadline for selector regex transforms |
| `SCRAPEYARD_TRANSFORM_REGEX_MAX_PATTERN_BYTES` | `2048` | Maximum UTF-8 size of a selector regex pattern |
| `SCRAPEYARD_TRANSFORM_MAX_PIPELINE_STEPS` | `32` | Maximum transforms in one selector pipeline |
| `SCRAPEYARD_TRANSFORM_MAX_VALUE_BYTES` | `1048576` | Maximum UTF-8 bytes for each intermediate selector value |
| `SCRAPEYARD_WEBHOOK_MAX_DELIVERY_ATTEMPTS` | `5` | Total durable delivery attempts, including the first |
| `SCRAPEYARD_WEBHOOK_MAX_DELIVERY_AGE_SECONDS` | `86400` | Maximum age from durable intent creation to another attempt |
| `SCRAPEYARD_WEBHOOK_DISPATCH_CONCURRENCY` | `4` | Maximum simultaneous webhook HTTP attempts |
| `SCRAPEYARD_WEBHOOK_DISPATCH_BATCH_SIZE` | `100` | Maximum due rows loaded into one bounded dispatch batch |
| `SCRAPEYARD_WEBHOOK_DELIVERED_RETENTION_DAYS` | `7` | Full delivered-row inspection window before secret scrubbing |
| `SCRAPEYARD_WEBHOOK_FAILED_RETENTION_DAYS` | `30` | Full failed-row inspection window before secret scrubbing |
| `SCRAPEYARD_MAX_REQUEST_BYTES` | `262144` | Max request body size |

See [src/scrapeyard/common/settings.py](src/scrapeyard/common/settings.py) for
the full settings surface.

Run duration, fetched-byte, record, and serialized-result budget violations
always finish the run as `failed`, regardless of `execution.fail_strategy`.
The job, run, compact failure result, and a `budget_exceeded` error remain
queryable. Structured error details include `limit_name`, `configured_limit`,
and `observed_amount`. Exact-boundary payloads are accepted; the first
over-limit reservation stops remaining target work.

Fetched bytes are measured from actual response-body bytes on the `basic`
fetch path, including redirects and retry responses. Scrapling's browser APIs
do not expose reliable aggregate network-transfer totals, so `dynamic` and
`stealthy` traffic is not included in this counter. Browser diagnostics have a
separate budget: excerpts are truncated or omitted and screenshots are omitted
before writing when they do not fit. These diagnostic omissions do not fail an
otherwise successful run.

Durable webhook delivery uses a fixed worker pool and bounded due-row batches.
Retries stop at the configured total-attempt or persisted-age boundary, honor
safe `Retry-After` values for 429/503 responses, and retain inspectable failed
rows. After the delivered/failed retention window, secret-bearing columns are
scrubbed in place while the deterministic identity and terminal status remain
as reconciliation deduplication evidence. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)
for exact boundaries, dead-letter reasons, restart semantics, and limitations.

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

- Set `SCRAPEYARD_API_CREDENTIALS` before exposing protected endpoints.
- Keep port `8420` private. Scrapeyard is designed to be consumed by Eyebox or
  another trusted internal service, not exposed as a public API.
- Use persistent storage for `/data` and Redis append-only data.
- Treat the current service as single-instance. The queue is Redis-backed, but
  SQLite stores and local result artifacts are not a horizontally scaled
  deployment model. A second process sharing `SCRAPEYARD_DB_DIR` is rejected
  by the kernel-backed instance lock, and multi-worker server settings fail
  startup. See [docs/SCALING.md](docs/SCALING.md) for state ownership and the
  future service-split boundary.
- Store secrets in environment variables or an orchestrator secret store.
- Follow [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) before promoting a runtime
  environment.
