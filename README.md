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
export SCRAPEYARD_HEALTH_PROBE_API_KEY="$(openssl rand -hex 32)"
printf -v SCRAPEYARD_API_CREDENTIALS \
  '{"local-admin":{"identity":"local-admin","secret":"%s","scopes":["submit","read","schedule-admin","delete","transport-admin"]},"local-health":{"secret":"%s","scopes":["health-detail"]}}' \
  "$SCRAPEYARD_API_KEY" "$SCRAPEYARD_HEALTH_PROBE_API_KEY"
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
curl -s -H "X-API-Key: $SCRAPEYARD_HEALTH_PROBE_API_KEY" \
  http://127.0.0.1:8420/health/ready
```

## Docker

Start the full local stack:

```bash
export SCRAPEYARD_ENCRYPTION_ACTIVE_KEY_ID=local-v1
export SCRAPEYARD_ENCRYPTION_KEYS="{\"local-v1\":\"$(openssl rand -base64 32 | tr -d '\n')\"}"
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build
```

Stop it:

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml down
```

The Compose setup starts Scrapeyard and Redis and mounts persistent data at
`/data`. The local overlay deliberately enables
`SCRAPEYARD_LOCAL_DEVELOPMENT_UNAUTHENTICATED=true` and clears credentials.
Never use that overlay in production. Source launches and production Compose
fail startup when credentials or the dedicated health-probe credential are
missing or malformed.

The production base Compose file exposes no host port. The explicit local
override publishes `127.0.0.1:8420` by default. Set
`SCRAPEYARD_BIND_ADDRESS=0.0.0.0` only on a trusted, firewalled development
host when peer containers must use the host gateway; a shared private Docker
network is preferred. The local override explicitly selects trusted-input mode
and disables the production egress-policy attestation.

For a deployment that accepts untrusted scrape YAML, configure an operator
proxy that rejects private, link-local, and metadata targets, then use the
root deployment transaction instead of calling `docker compose up` directly:

```bash
export SCRAPEYARD_PROXY_URL=https://scrape-proxy.example:8443
export SCRAPEYARD_EGRESS_ALLOW_CIDRS=203.0.113.10/32  # proxy address, if needed
export SCRAPEYARD_IMAGE=ghcr.io/example/scrapeyard@sha256:<qualified-digest>
# Also export API/health credentials and the encryption keyring from the
# deployment secret store.
sudo --preserve-env=SCRAPEYARD_IMAGE,SCRAPEYARD_PROXY_URL,SCRAPEYARD_EGRESS_ALLOW_CIDRS,SCRAPEYARD_API_CREDENTIALS,SCRAPEYARD_HEALTH_PROBE_API_KEY,SCRAPEYARD_ENCRYPTION_KEYS,SCRAPEYARD_ENCRYPTION_ACTIVE_KEY_ID \
  security/deploy-secure-compose.sh
```

The production defaults require that proxy and a connected-IP policy probe.
Startup fails if the proxy is empty/`direct`, if the controlled helper's
liveness protocol is unavailable before or after the policy challenge, or if
the private challenge port is reachable. See
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#egress) for the full host-policy and
decommissioning procedure.

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
Patchright Chromium for `fetcher: dynamic` with `browser.stealth: true`, and
the maintained Camoufox package/assets for `fetcher: stealthy`. Browser
versions, revisions, primary upstream sources, review/expiry dates, and the
Camoufox archive checksum are enforced by `security/browser-policy.json`.
The built image executes both browser binaries, records their reported versions
in `/usr/share/scrapeyard-browser-runtime.json`, and the container SBOM adds
those binaries explicitly. The image starts directly
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

Protected endpoints always require `X-API-Key` unless the explicit
`SCRAPEYARD_LOCAL_DEVELOPMENT_UNAUTHENTICATED=true` local-only mode is enabled.
Missing credentials never imply access. Named credentials declare `submit`, `read`, `schedule-admin`, `delete`,
`health-detail`, and/or `transport-admin` scopes and may be restricted to
project names. Reserve `transport-admin` for operators: in untrusted-submission
mode it permits caller-selected proxy and CDP transports.
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

Per-domain rate limits, circuit breakers, artifact directories, and result
source labels share one canonical URL identity. Hostnames are lowercased and
IDNA-encoded, trailing dots and explicit default HTTP/HTTPS ports are removed,
and non-default ports remain separate origins.

Configuration YAML rejects aliases, duplicate keys, and collection nesting
beyond 50 levels. Excessive nesting is reported as a sanitized HTTP 422 rather
than an internal parser error.

Common target fields:

| Field | Purpose |
| --- | --- |
| `url` | Target URL |
| `fetcher` | `basic`, `stealthy`, or `dynamic` |
| `selectors` | Output fields mapped to CSS or XPath selectors |
| `item_selector` | Optional repeated-item container selector |
| `pagination` | Optional next-page selector and total page limit (`max_pages` is at least 1) |
| `browser` | Optional browser runtime controls and pre-extraction actions for `dynamic` or `stealthy` fetches; rejected for `basic` |
| `proxy` | Optional per-target proxy override |
| `map_detection` | Optional pricing visibility detection rules |
| `stock_detection` | Optional stock status detection rules |

`execution.fail_strategy: all_or_nothing` publishes records only when every
target succeeds. If any target fails, both grouping modes persist an empty
`results` value and all database/webhook record counts are zero; redacted
per-target diagnostics remain available under `targets`.

Selector transforms can be chained with `|`. Supported string transforms are
`trim`, `collapse_whitespace`, `lowercase`, `uppercase`, `prepend`, `append`,
`replace`, `remove`, `strip_prefix`, `strip_suffix`, `regex`, `extract`, and
`default`.

Each target accepts at most 100 selectors. Selector field names are limited to
256 characters and CSS/XPath queries to 4,096 characters. The historical
`SCRAPEYARD_TRANSFORM_MAX_VALUE_BYTES` setting applies to every extracted
selector value, including selectors without transforms. Values are measured as
UTF-8 bytes. During extraction, the run also reserves the compact JSON size of
each result value against `SCRAPEYARD_RUN_MAX_SERIALIZED_RESULT_BYTES`; an exact
check of the complete artifact remains in place before it is written.

MAP and per-status stock detection accept at most 100 text patterns and 50 CSS
selectors per list. Text/price patterns are limited to 512 characters;
detection CSS uses the normal 4,096-character selector limit. Blank text/CSS
patterns and duplicates are rejected, while an explicitly empty MAP
`price_value_patterns` entry remains supported for sites that emit an empty raw
price. Validation accepts at most 100 unique, nonblank `required_fields`.
Extraction/detection and validation run outside the event loop under the one
run deadline, with deadline checkpoints inside record/pattern loops.

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
Each action rejects fields it does not use: `times` and `pixels` belong to
`scroll`, `max_times` belongs to `repeat_click`, and the post-click
`wait_for_selector` is accepted by `click` and `repeat_click`. Numeric controls
must be YAML numbers rather than booleans.
For `fetcher: stealthy`, `browser.additional_arguments` deliberately accepts
only YAML-safe Camoufox overrides: `locale` (one tag or a list), `fonts` (a
bounded list of names), `custom_fonts_only` (which requires `fonts`), and
`window` (a two-integer `[width, height]` list). Python-object controls such as
custom fingerprints and screen constraint objects are not accepted by the
service schema.
`browser.humanize` accepts literal `true`/`false` or a positive, finite maximum
cursor-movement duration no greater than 60 seconds.

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

`execution.domain_rate_limit` is the minimum interval between top-level
fetch/navigation attempts to the same canonical host. It applies to initial
fetches, retries, redirect hops, pagination, and validation retries. Browser
subresources are not throttled by this setting.

`retry.max_attempts` is the total number of attempts per request, including the
initial attempt. Connection and timeout failures follow the same backoff,
cancellation, rate-limit, metric, and run-budget policy as retryable HTTP statuses.

Terminal webhook intent is durable before a run is reported complete. Each
logical event uses a stable `whv1_<sha256>` delivery ID derived only from its
job ID, run ID, and event, and the same ID is sent to the receiver for
deduplication. HTTP delivery remains asynchronous and at-least-once; startup
repairs a missing terminal intent and replays pending rows without resetting
already delivered or permanently failed deliveries. See
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for the transaction and cross-database
consistency boundaries.

The six-hour cleanup cycle applies normal result retention first, then validates
metadata-backed artifacts and reconciles stale filesystem orphans and known
atomic-write temporary files. Reconciliation is dry-run by default. A
metadata row remains authoritative even after its parent job is deleted with
`delete_results=false`; missing, unsafe, unreadable, or corrupt `results.json`
files are reported as storage failures and are not repaired or deleted. See
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#result-artifact-reconciliation) before
enabling destructive reconciliation.

Repeated full retention and artifact-reconciliation cursor pages drain within
per-category item and shared-time budgets. Metadata and filesystem scans report
exhaustion independently, persist their keyset cursors across restarts, and a
saturated cycle resumes after a short catch-up delay rather than the normal
interval. The same cycle bounds operational history. Old terminal ad-hoc jobs are removed
through resumable deletion while their result metadata/artifacts follow the
separate result policy. Scheduled run rows, their reconciled webhook
tombstones, and error rows are pruned in bounded batches. API `run_count` and
`last_run_at` are lifetime summaries and do not shrink when detailed run
history is compacted.

## Settings

All service settings use the `SCRAPEYARD_` prefix. The most commonly changed
settings are:

The base Compose deployment passes every runtime setting through from the shell
or `.env` while retaining its checked-in default. The three
`SCRAPEYARD_QUALIFICATION_*` settings are intentionally unavailable there; they
are reserved for the destructive, local-only release-qualification Compose
profile.

| Variable | Default | Purpose |
| --- | --- | --- |
| `SCRAPEYARD_API_CREDENTIALS` | empty | Named JSON credentials with identity, secret, scopes, and optional projects |
| `SCRAPEYARD_API_KEYS` | empty | Deprecated full-admin migration allow-list; remove after converting to named credentials |
| `SCRAPEYARD_LOCAL_DEVELOPMENT_UNAUTHENTICATED` | `false` | Explicit local-only full-admin opt-in; cannot be combined with credentials or a health key |
| `SCRAPEYARD_HEALTH_PROBE_API_KEY` | empty | Required production probe secret; must match a credential with only `health-detail` and no project restriction |
| `SCRAPEYARD_SECRET_REFERENCE_ALLOWLIST` | empty | JSON map of project names to permitted `SCRAPEYARD_SECRET_*` references; `*` defines explicitly shared names |
| `SCRAPEYARD_ENCRYPTION_KEYS` | empty | Required JSON key-ID to base64 32-byte AES key map for persisted secrets; startup fails while empty |
| `SCRAPEYARD_ENCRYPTION_ACTIVE_KEY_ID` | empty | Required key ID used for new writes and startup rotation |
| `SCRAPEYARD_HEALTH_PROBE_TIMEOUT_SECONDS` | `2` | Per-operation timeout for detailed readiness probes and metric snapshots |
| `SCRAPEYARD_UNTRUSTED_SUBMISSIONS` | `false` (`true` in production Compose) | Require the operator proxy and policy probe; restrict submitted proxy/CDP overrides to `transport-admin` callers |
| `SCRAPEYARD_PROXY_URL` | empty | Operator-controlled default proxy; required and cannot be `direct` in untrusted-submission mode |
| `SCRAPEYARD_EGRESS_POLICY_PROBE_HOST` | empty (`172.30.0.248` in production Compose) | Controlled non-public numeric address hosting the two-channel policy probe |
| `SCRAPEYARD_EGRESS_POLICY_PROBE_PORT` | `0` (`8080` in production Compose) | Challenge port that must be denied while the helper is live |
| `SCRAPEYARD_EGRESS_POLICY_PROBE_LIVENESS_PORT` | `0` (`8081` in production Compose) | Narrowly allowed protocol port proving the helper and challenge listener remain live |
| `SCRAPEYARD_EGRESS_POLICY_PROBE_TIMEOUT_SECONDS` | `1` | Bounded startup attempt for the connected-IP policy probe |
| `SCRAPEYARD_METRICS_REFRESH_INTERVAL_SECONDS` | `5` | Minimum interval between durable Prometheus gauge refreshes |
| `SCRAPEYARD_REDIS_DSN` | `redis://redis:6379/0` | Redis connection for `arq` |
| `SCRAPEYARD_QUEUE_NAME` | `scrapeyard` | Base arq execution queue; priority intake queues append `:priority:high`, `:priority:normal`, and `:priority:low` |
| `SCRAPEYARD_DB_DIR` | `/data/db` | SQLite database directory |
| `SCRAPEYARD_STORAGE_RESULTS_DIR` | `/data/results` | Result artifact directory |
| `SCRAPEYARD_STORAGE_CLEANUP_BATCH_SIZE` | `500` | Maximum result metadata rows or filesystem entries processed per bounded transaction/reconciliation cursor page |
| `SCRAPEYARD_STORAGE_CLEANUP_CYCLE_MAX_ITEMS_PER_PHASE` | `10000` | Maximum eligible items drained per retention category in one maintenance cycle |
| `SCRAPEYARD_STORAGE_CLEANUP_CYCLE_MAX_SECONDS` | `60` | Shared elapsed-time ceiling for one cleanup cycle |
| `SCRAPEYARD_STORAGE_CLEANUP_CATCHUP_DELAY_SECONDS` | `5` | Delay before another cycle when a cleanup budget is saturated |
| `SCRAPEYARD_STORAGE_ORPHAN_GRACE_SECONDS` | `86400` | Minimum artifact age before orphan/temp removal eligibility |
| `SCRAPEYARD_STORAGE_RECONCILIATION_DRY_RUN` | `true` | Report eligible orphan/temp removals without changing files |
| `SCRAPEYARD_STORAGE_RECONCILIATION_PROMOTION_ACK` | empty | Zoned RFC 3339 operator timestamp required before destructive reconciliation can start |
| `SCRAPEYARD_ADAPTIVE_DIR` | `/data/adaptive` | Scrapling adaptive state directory |
| `SCRAPEYARD_LOG_DIR` | `/data/logs` | Log directory |
| `SCRAPEYARD_SYNC_TIMEOUT_SECONDS` | `15` | Max wait for sync scrape responses |
| `SCRAPEYARD_SYNC_POLL_DELAY_SECONDS` | `0.5` | Sync response polling interval |
| `SCRAPEYARD_IDEMPOTENCY_KEY_MAX_BYTES` | `128` | Maximum visible-ASCII idempotency key length |
| `SCRAPEYARD_IDEMPOTENCY_RETENTION_HOURS` | `24` | Caller/key replay and conflict window |
| `SCRAPEYARD_IDEMPOTENCY_CLEANUP_BATCH_SIZE` | `1000` | Maximum expired key records removed per bounded cleanup transaction |
| `SCRAPEYARD_RATE_LIMIT_REQUESTS` | `600` | Maximum API requests per caller in one sliding window; `0` disables the limiter |
| `SCRAPEYARD_RATE_LIMIT_WINDOW_SECONDS` | `60` | API sliding-window length in seconds |
| `SCRAPEYARD_RATE_LIMIT_MAX_KEYS` | `10000` | Maximum live API-key/client-IP rate-limit buckets; new identities are refused at saturation |
| `SCRAPEYARD_DOMAIN_RATE_LIMIT_SHARED` | `true` | Use Redis for cross-job domain pacing when the queue connection is available |
| `SCRAPEYARD_DOMAIN_RATE_LIMIT_MAX_DOMAINS` | `10000` | Maximum process-local domain pacing entries when shared pacing is disabled |
| `SCRAPEYARD_HISTORY_ADHOC_JOB_RETENTION_DAYS` | `30` | Terminal ad-hoc job history window before resumable metadata deletion |
| `SCRAPEYARD_HISTORY_SCHEDULED_RUN_RETENTION_DAYS` | `30` | Scheduled run history age window |
| `SCRAPEYARD_HISTORY_SCHEDULED_RUN_RETENTION_COUNT` | `100` | Maximum newest scheduled runs retained per job; age expiry may retain fewer |
| `SCRAPEYARD_HISTORY_ERROR_RETENTION_DAYS` | `30` | Structured error history window |
| `SCRAPEYARD_HISTORY_WEBHOOK_TOMBSTONE_RETENTION_DAYS` | `30` | Scrubbed tombstone window before atomic run/tombstone compaction |
| `SCRAPEYARD_HISTORY_ADHOC_JOB_CLEANUP_BATCH_SIZE` | `100` | Maximum ad-hoc jobs selected per bounded cleanup transaction |
| `SCRAPEYARD_HISTORY_SCHEDULED_RUN_CLEANUP_BATCH_SIZE` | `500` | Maximum scheduled runs selected per bounded cleanup transaction |
| `SCRAPEYARD_HISTORY_ERROR_CLEANUP_BATCH_SIZE` | `1000` | Maximum error rows removed per cleanup operation |
| `SCRAPEYARD_SCHEDULER_MISFIRE_GRACE_SECONDS` | `60` | Maximum lateness for one coalesced in-process cron fire |
| `SCRAPEYARD_WORKERS_MAX_CONCURRENT` | `4` | Max concurrent jobs |
| `SCRAPEYARD_WORKERS_MAX_BROWSERS` | `2` | Max concurrent browser targets across all jobs |
| `SCRAPEYARD_WORKERS_CANCELLATION_GRACE_SECONDS` | `10` | Bounded arq abort and worker-quiescence wait for cancellation |
| `SCRAPEYARD_WORKERS_QUEUED_CLAIM_TIMEOUT_SECONDS` | `300` | Age after which an unclaimed queued delivery may be replaced |
| `SCRAPEYARD_WORKERS_QUEUE_PAYLOAD_TTL_SECONDS` | `604800` | Redis payload lifetime and maximum uninterrupted priority-queue residence before repair |
| `SCRAPEYARD_WORKERS_QUEUED_RECONCILIATION_INTERVAL_SECONDS` | `60` | Interval between bounded repairs of stale accepted queue deliveries |
| `SCRAPEYARD_WORKERS_QUEUED_RECONCILIATION_BATCH_SIZE` | `100` | Maximum stale queued SQLite runs inspected per periodic repair pass |
| `SCRAPEYARD_WORKERS_RUNNING_HEARTBEAT_TIMEOUT_SECONDS` | `600` | Time since the last persisted run heartbeat before recovery is allowed |
| `SCRAPEYARD_WORKERS_RUNNING_RECONCILIATION_INTERVAL_SECONDS` | `60` | Interval between bounded stale-running and terminal-intent repair passes |
| `SCRAPEYARD_WORKERS_RUNNING_RECONCILIATION_BATCH_SIZE` | `100` | Maximum stale-running and terminal-intent rows processed per repair pass |
| `SCRAPEYARD_WORKERS_HEARTBEAT_INTERVAL_SECONDS` | `30` | Monotonic interval between persisted run heartbeats; at most one third of the running timeout |
| `SCRAPEYARD_RUN_MAX_DURATION_SECONDS` | `900` | Overall monotonic deadline for one run |
| `SCRAPEYARD_RUN_MAX_FETCHED_BYTES` | `104857600` | Aggregate encoded basic-response body bytes, checked after each response |
| `SCRAPEYARD_RUN_MAX_EXTRACTED_RECORDS` | `100000` | Aggregate extracted records across targets, pages, and validation retries |
| `SCRAPEYARD_RUN_MAX_SERIALIZED_RESULT_BYTES` | `52428800` | Aggregate extracted JSON growth ceiling plus exact maximum UTF-8 bytes for persisted result JSON |
| `SCRAPEYARD_RUN_MAX_BROWSER_DEBUG_BYTES` | `26214400` | Aggregate browser excerpt and screenshot bytes per run |
| `SCRAPEYARD_RUN_THREAD_MAX_WORKERS` | `4` | Dedicated bounded capacity for run-scoped selector, validation, and DNS thread work |
| `SCRAPEYARD_API_RESULT_THREAD_MAX_WORKERS` | `2` | Dedicated bounded capacity for off-loop result response normalization and JSON rendering |
| `SCRAPEYARD_TRANSFORM_REGEX_TIMEOUT_SECONDS` | `0.1` | Per-operation deadline for selector regex transforms |
| `SCRAPEYARD_TRANSFORM_REGEX_MAX_PATTERN_BYTES` | `2048` | Maximum UTF-8 size of a selector regex pattern |
| `SCRAPEYARD_TRANSFORM_MAX_PIPELINE_STEPS` | `32` | Maximum transforms in one selector pipeline |
| `SCRAPEYARD_TRANSFORM_MAX_VALUE_BYTES` | `1048576` | Historical name for the maximum UTF-8 bytes of every selector value, with or without transforms |
| `SCRAPEYARD_CIRCUIT_BREAKER_MAX_FAILURES` | `3` | Consecutive transient upstream failures before opening a domain circuit |
| `SCRAPEYARD_CIRCUIT_BREAKER_COOLDOWN_SECONDS` | `300` | Open interval before exactly one half-open probe is admitted |
| `SCRAPEYARD_CIRCUIT_BREAKER_MAX_DOMAINS` | `10000` | Maximum retained per-domain circuit states |
| `SCRAPEYARD_CIRCUIT_BREAKER_INACTIVE_TTL_SECONDS` | `3600` | Idle lifetime for sub-threshold circuit history |
| `SCRAPEYARD_WEBHOOK_MAX_DELIVERY_ATTEMPTS` | `5` | Total durable delivery attempts, including the first |
| `SCRAPEYARD_WEBHOOK_MAX_DELIVERY_AGE_SECONDS` | `86400` | Maximum age from durable intent creation to another attempt |
| `SCRAPEYARD_WEBHOOK_DISPATCH_CONCURRENCY` | `4` | Maximum simultaneous webhook HTTP attempts |
| `SCRAPEYARD_WEBHOOK_DISPATCH_BATCH_SIZE` | `100` | Maximum due rows loaded into one bounded dispatch batch |
| `SCRAPEYARD_WEBHOOK_CLIENT_CACHE_MAX_SIZE` | `64` | Maximum hostname-isolated webhook HTTP client pools |
| `SCRAPEYARD_WEBHOOK_CLIENT_CACHE_IDLE_TTL_SECONDS` | `300` | Idle lifetime for a cached webhook HTTP client pool |
| `SCRAPEYARD_WEBHOOK_DELIVERED_RETENTION_DAYS` | `7` | Full delivered-row inspection window before secret scrubbing |
| `SCRAPEYARD_WEBHOOK_FAILED_RETENTION_DAYS` | `30` | Full failed-row inspection window before secret scrubbing |
| `SCRAPEYARD_MAX_REQUEST_BYTES` | `262144` | Max request body size |

See [src/scrapeyard/common/settings.py](src/scrapeyard/common/settings.py) for
the full settings surface.

Circuit breakers count connection/network errors, timeouts, HTTP 429, and HTTP
5xx responses. Selector, validation, budget, cancellation, local browser, and
ordinary non-retryable HTTP 4xx failures remain observable errors but do not
affect shared domain availability. After cooldown, one half-open probe is
admitted; its transient failure starts a fresh cooldown and its successful
upstream response closes the circuit.

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
poetry run ruff check src tests scripts
poetry run mypy src/scrapeyard
poetry run python scripts/audit_dependencies.py all
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
