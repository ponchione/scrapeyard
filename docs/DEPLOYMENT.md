# Deployment Hardening

Scrapeyard is intended to run as an internal worker service consumed by
Eyebox, not as a public internet API. The deployment should enforce that
assumption at the network layer.

## Required Topology

- Eyebox is the only service that can reach Scrapeyard HTTP traffic.
- Redis is private to Scrapeyard and is not exposed outside the internal
  runtime network.
- Scrapeyard stores `/data` on persistent storage.
- Browser scraping egress is controlled by the host, orchestrator, proxy
  gateway, or firewall policy.

## Ingress

- Do not make `8420` reachable from untrusted networks.
- The container process listens on `0.0.0.0:8420`. The bundled local Docker
  Compose file publishes the host port on `0.0.0.0:8420` so Eyebox containers
  in another local Compose stack can fetch `http://host.docker.internal:8420`.
  Use this only on trusted local or private hosts with firewall rules limiting
  ingress.
- If only host-local tools should reach Scrapeyard, override the Compose bind:

  ```yaml
  ports:
    - "127.0.0.1:8420:8420"
  ```

- For a shared deployment with Eyebox, prefer a private service network over a
  host port. If Eyebox shares Scrapeyard's Compose network, use
  `http://scrapeyard:8420` instead of `host.docker.internal`. If a reverse proxy
  is used, restrict the proxy route to Eyebox and keep `/health` available only
  to internal monitoring.
- Always set `SCRAPEYARD_API_KEYS`; the app permits unauthenticated requests
  only when this value is empty for local development.

## Egress

App-level URL guards are a backstop, not the only control. Enforce outbound
network policy for the Scrapeyard container or pod:

- Allow Redis.
- Allow the configured proxy gateway if scraping through a proxy.
- Allow public HTTP/HTTPS destinations needed for scraping.
- Block cloud metadata and link-local ranges, including `169.254.169.254` and
  `169.254.0.0/16`.
- Block private/internal ranges unless explicitly required:
  `127.0.0.0/8`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, IPv6
  loopback, link-local, and ULA ranges.

If practical, route all scrape traffic through a single proxy gateway and allow
Scrapeyard egress only to that gateway plus Redis. That is the strongest
deployment boundary because browser runtimes and DNS behavior stay outside the
application trust boundary.

## Secrets

- Generate API keys with a high-entropy value:

  ```bash
  openssl rand -hex 32
  ```

- Store `SCRAPEYARD_API_KEYS` in the deployment secret store, not in source
  control.
- Rotate the key with Eyebox credentials. During rotation, set both old and new
  keys as a comma-separated list, deploy Eyebox with the new key, then remove
  the old key.
- Treat proxy URLs, webhook headers, and browser extra headers as secrets.

## Persistence And Backups

Persist and back up:

- `/data/db`
- `/data/results`
- `/data/adaptive`
- `/data/logs` if logs are not shipped elsewhere
- Redis append-only data if queued jobs must survive Redis restarts

SQLite files can be backed up online with `.backup`, or by snapshotting the
persistent volume. Include all database files:

```bash
sqlite3 /data/db/jobs.db ".backup '/backup/jobs.db'"
sqlite3 /data/db/errors.db ".backup '/backup/errors.db'"
sqlite3 /data/db/results_meta.db ".backup '/backup/results_meta.db'"
```

Restore testing matters. A deployment is not production-ready until a restored
`/data/db` plus `/data/results` can serve `GET /jobs/{job_id}` and
`GET /results/{job_id}` for a known completed scrape.

## Runtime Limits

Set limits to match host capacity:

- `SCRAPEYARD_WORKERS_MAX_CONCURRENT`
- `SCRAPEYARD_WORKERS_MAX_BROWSERS`
- `SCRAPEYARD_WORKERS_MEMORY_LIMIT_MB`
- `SCRAPEYARD_WORKERS_CANCELLATION_GRACE_SECONDS` (default `10`)
- `SCRAPEYARD_WORKERS_QUEUED_CLAIM_TIMEOUT_SECONDS` (default `300`)
- `SCRAPEYARD_WORKERS_RUNNING_HEARTBEAT_TIMEOUT_SECONDS` (default `600`)
- `SCRAPEYARD_WORKERS_HEARTBEAT_INTERVAL_SECONDS` (default `30`)
- `SCRAPEYARD_MAX_REQUEST_BYTES`
- `SCRAPEYARD_RUN_MAX_DURATION_SECONDS` (default `900`)
- `SCRAPEYARD_RUN_MAX_FETCHED_BYTES` (default `104857600`, 100 MiB)
- `SCRAPEYARD_RUN_MAX_EXTRACTED_RECORDS` (default `100000`)
- `SCRAPEYARD_RUN_MAX_SERIALIZED_RESULT_BYTES` (default `52428800`, 50 MiB)
- `SCRAPEYARD_RUN_MAX_BROWSER_DEBUG_BYTES` (default `26214400`, 25 MiB)
- `SCRAPEYARD_WEBHOOK_MAX_DELIVERY_ATTEMPTS` (default `5`)
- `SCRAPEYARD_WEBHOOK_MAX_DELIVERY_AGE_SECONDS` (default `86400`)
- `SCRAPEYARD_WEBHOOK_DISPATCH_CONCURRENCY` (default `4`)
- `SCRAPEYARD_WEBHOOK_DISPATCH_BATCH_SIZE` (default `100`, and no smaller than concurrency)
- `SCRAPEYARD_WEBHOOK_DELIVERED_RETENTION_DAYS` (default `7`)
- `SCRAPEYARD_WEBHOOK_FAILED_RETENTION_DAYS` (default `30`)
- `SCRAPEYARD_STORAGE_RETENTION_DAYS`
- `SCRAPEYARD_STORAGE_MAX_RESULTS_PER_JOB`
- `SCRAPEYARD_STORAGE_ORPHAN_GRACE_SECONDS` (default `86400`)
- `SCRAPEYARD_STORAGE_RECONCILIATION_DRY_RUN` (default `true`)
- `SCRAPEYARD_HEALTH_DISK_FREE_MIN_MB`

Queued delivery replacement and running execution recovery use independent
clocks. `SCRAPEYARD_WORKERS_QUEUED_CLAIM_TIMEOUT_SECONDS` applies only while a
delivery has not been claimed. Once a worker atomically claims a run, its
persisted `heartbeat_at` is authoritative and the scheduler may recover it only
after `SCRAPEYARD_WORKERS_RUNNING_HEARTBEAT_TIMEOUT_SECONDS` without a
successful refresh. The heartbeat interval must be no more than one third of
the running timeout.

Operational cancellation and deletion semantics, including fail-closed Redis
inspection, resumable `deleting` reservations, result preservation, webhook
conflicts, and cross-database crash boundaries, are documented in
[JOB_LIFECYCLE.md](JOB_LIFECYCLE.md). Cancellation uses the dedicated worker
grace above and does not reuse the broader shutdown drain window.

### Priority queue operation

`SCRAPEYARD_QUEUE_NAME` names the arq execution queue and is also the prefix
for three intake sorted sets. For a base name `scrapeyard`, the fixed queue set
is:

- `scrapeyard:priority:high`
- `scrapeyard:priority:normal`
- `scrapeyard:priority:low`
- `scrapeyard` (admitted execution and upgrade-compatible legacy deliveries)

Ad-hoc submission, scheduled submission, and missing-delivery recovery derive
priority from the same validated stored configuration and enqueue the accepted
`run_id` into the corresponding intake queue. The enqueue transaction checks
arq's global job and result keys, so the first accepted `run_id` wins even when
a duplicate attempt requests another priority. The duplicate receives a
queue-aware result handle for that same logical delivery; it is not rerouted.
Within a priority, Redis atomically assigns increasing sorted-set scores, so
the Redis acceptance order is FIFO.

The one embedded arq Worker admits only enough work to fill its currently
available global handler slots. Its work-conserving fair-turn cycle is:

```text
high, high, high, high, normal, normal, low
```

The named turn is preferred when several queues are non-empty. If that queue
is empty, the dispatcher selects the highest non-empty priority and still
advances the cycle. When all priorities remain backlogged, normal therefore
receives two of every seven admissions and low one of every seven. A waiting
normal or low delivery receives a turn within at most seven subsequent
admission decisions, meaning at most six other deliveries can be admitted
first. Work already admitted to the base queue or already running is not
preempted, so no wall-clock service bound can be tighter than the time needed
for handler capacity to become available. Size run-duration budgets as the
outer bound on an individual cooperative handler; external stalls and process
failure can still extend observed queue delay.

There are not three arq workers. One Worker owns the global
`SCRAPEYARD_WORKERS_MAX_CONCURRENT` counter, one process-wide browser limiter
covers every priority, and one shutdown path stops admission before draining
or cancelling active work under the existing grace. Increasing job capacity
therefore does not multiply by three. Browser capacity is independent and is
still measured at active browser-target execution, not at queued job level.

Redis holds all four known sorted sets and the global arq payload/result keys.
On restart, already admitted base-queue work is consumed before additional
intake admission; priority intake and same-priority FIFO scores survive only to
the extent Redis persistence does. The in-memory fair-turn cursor restarts at
the first high turn. One restart can therefore postpone a lower-priority turn
by at most another cycle, but repeated restarts before that turn can extend the
delay. The dispatcher, SQLite stores, local results, scheduler, and global
capacity accounting retain the service's single-process/single-instance
assumption; running multiple Scrapeyard instances would multiply capacity and
would not provide one shared fairness cursor.

`/health` exposes `workers.queue_depths` with the exact keys `high`, `normal`,
and `low`. Each value is a direct `ZCARD` of its known intake queue and includes
waiting/deferred members, including an orphaned member until reconciliation,
but excludes admitted base-queue and in-progress work. `active_tasks` remains
the separate running-handler count. If Redis cannot provide these bounded
depth reads, all three values are `null`, the Redis dependency is unhealthy,
and `/health` returns 503.

Heartbeat writes are serialized per run. One write failure does not abandon a run:
the worker retries on the next monotonic interval and logs only job/run
lease timing and the exception type. If storage keeps failing until the running
timeout elapses, or storage rejects the expected `run_id`, the worker cancels
its active target work, releases browser permits, discards any unfinalized
run-specific result artifact, and performs no webhook or terminal mutation.
The stale database state then remains recoverable by the conditional scheduler
or startup pass. Recovery and finalization update the run and parent job in one
SQLite transaction, so a racing successful heartbeat or terminal update turns
recovery into a no-op.

Startup also reconciles queued SQLite ownership after Redis connects and before
APScheduler starts. Only queued rows older than
`SCRAPEYARD_WORKERS_QUEUED_CLAIM_TIMEOUT_SECONDS` with a persisted
`current_run_id` are inspected. Scrapeyard directly checks that run ID through
arq's global result, in-progress, and payload keys plus the fixed four-queue
set above. This is one bounded transaction and never scans Redis keys. Queued,
deferred, and in-progress deliveries are left untouched.

If the arq delivery is missing, both ad-hoc and scheduled jobs are re-enqueued
from their stored YAML with the original run ID. Priority and browser metadata
are rebuilt through the same policy as normal submission. For scheduled jobs,
`schedule_enabled` controls future cron triggers; disabling a schedule does not
discard a delivery that was already accepted. Reconciliation conditionally
reserves the exact queued timestamp and run ownership it inspected, so a worker
claim or run replacement makes a stale recovery attempt a no-op. A completed
arq record paired with queued SQLite state, an invalid stored config, or a
recovery enqueue failure conditionally fails only that still-current queued
run. Redis inspection failure aborts startup before the scheduler can fire,
because continuing without authoritative queue state could duplicate work.

Size browser concurrency for the host independently from job concurrency. The
limit applies to active `dynamic` and `stealthy` targets across all jobs; basic
targets do not consume browser permits.

The run deadline is one monotonic deadline shared by target concurrency,
pagination, retry backoff, domain-rate-limit waits, validation retry, browser
actions, and result preparation. Duration, fetched-byte, extracted-record, and
serialized-result exhaustion produces terminal status `failed` for every fail
strategy and a structured `budget_exceeded` error. Size these ceilings from
representative production results, then leave headroom for normal catalog
growth; do not set the serialized-result ceiling below 4096 bytes because a
compact terminal diagnostic must remain persistable.

The fetched-byte ceiling counts reliable body bytes for `basic` requests,
including redirects and retry responses. It is checked after each complete
response because Scrapling's basic API is not streaming. Browser-backed
`dynamic` and `stealthy` fetches do not expose reliable total transfer size and
are therefore excluded. Their stored debug excerpts and screenshots are
instead bounded by `SCRAPEYARD_RUN_MAX_BROWSER_DEBUG_BYTES`; omitted or
truncated captures are described in the result diagnostics without failing the
run. Result JSON is measured using the exact compact UTF-8 representation and
is written by temporary file plus atomic replacement before metadata commit.

### Result artifact reconciliation

`results_meta` is authoritative for retained result ownership. A valid row's
contained `file_path` names exactly one `project/job/run` directory below
`SCRAPEYARD_STORAGE_RESULTS_DIR`; that directory must contain a regular,
non-symlink `results.json` holding valid UTF-8 JSON. The parent job is not part
of this ownership decision. Deleting a job with `delete_results=false` leaves
its metadata and artifact authoritative and eligible for normal age/per-job
retention, not orphan removal.

The periodic cleanup order is expired-result retention, per-job result
pruning, artifact reconciliation, then webhook tombstone scrubbing. Retention
remains metadata-first, so a crash leaves a recoverable filesystem orphan.
Explicit job deletion remains filesystem-first and retryable. Running
reconciliation after retention avoids diagnosing directories the same pass is
already meant to remove normally.

The scanner treats the configured results directory as its sole deletion root.
It enumerates only regular, non-symlink directories at the exact
`project/job/run` depth, never follows project, job, run, nested-directory,
result-file, or temporary-file symlinks, and ignores non-directory or
unexpected-depth entries. Before a run is removed, its path is checked again
for containment and exact depth, all metadata paths are re-read, queued/running
ownership is queried again from `jobs.db`, and filesystem timestamps are
rechecked. Recursive byte accounting uses non-following stats; recursive
removal does not follow a symlink, so an external symlink target is untouched.

An unreferenced directory is eligible only when the run directory and every
non-followed contained entry are older than
`SCRAPEYARD_STORAGE_ORPHAN_GRACE_SECONDS` (24 hours by default). Exact
project, job-name, and `current_run_id` ownership in a queued or running job
protects the directory; Redis queue scans are not used. The age barrier also
protects browser-debug files created before result metadata commits and limits
the unavoidable SQLite/filesystem race.

Temporary cleanup accepts only the atomic writer's exact format with a
positive decimal PID and 32 lowercase hexadecimal UUID. Known targets are
`results.json`, `dynamic-main.png`, and `stealthy-main.png`, for example
`.results.json.1234.0123456789abcdef0123456789abcdef.tmp`. Arbitrary `.tmp`
files and lookalikes remain untouched. A stale temp inside an active or recent
run is protected; an eligible orphan run subsumes its temp into the contained
directory removal.

`SCRAPEYARD_STORAGE_RECONCILIATION_DRY_RUN=true` is the safe default. Each pass
performs the same validation and eligibility checks and reports what would be
removed without mutating files or metadata. Review at least one grace-period
window of summaries before setting it to `false`. Destructive passes remove
only eligible directories and known temp files, converge idempotently, and
never modify `results_meta`.

Every pass logs a typed summary: metadata inspected/valid, missing/corrupt/
unreadable/unsafe artifacts, filesystem runs inspected, orphan/temp candidates,
recent/active/race skips, proposed and completed removals, removed regular-file
bytes, dry-run state, and failure count. Per-entry reports contain only bounded
job/run or relative identifiers and exception types, never JSON contents,
stored YAML, secrets, raw exception text, or outside paths. Missing files,
invalid JSON, I/O failures, unsafe paths, symlinks, and non-regular result files
remain in metadata for operator diagnosis or the original explicit-deletion
retry. A top-level reconciliation exception is isolated so webhook scrubbing
can continue; cancellation propagates, and failures retry on the next six-hour
pass.

Scanning is proportional to retained metadata plus files below the results
root. JSON parsing, directory walks, non-following size accounting, and removal
run in worker threads. Allow I/O headroom and monitor cleanup duration and disk
capacity on large trees. There is no content repair, metadata synthesis,
filesystem-to-metadata recovery, or cross-instance reconciliation lease.
Filesystem mtimes can be coarse or administratively changed; permission
changes can make a candidate unverifiable; a path can still change after its
last check; and SQLite/filesystem operations are not one transaction. Active
and age rechecks reduce but cannot eliminate that gap. The service retains its
documented single-process/local-filesystem assumption.

## Terminal finalization and webhook intent

`jobs.db` is the authoritative atomic boundary for terminal execution state.
Normal, budget-exceeded, and in-process crash finalization use a
`BEGIN IMMEDIATE` compare-and-set transaction that updates the owned
`job_runs` row, updates the matching current `jobs` row, and inserts any
required `webhook_deliveries` row. The run must still be `running`, the parent
must still be `running` with the same `current_run_id`, and normal/budget
finalization also requires a heartbeat newer than its recovery cutoff. A stale
worker, racing heartbeat recovery, or superseding run therefore causes the
whole terminal mutation and intent insertion to roll back or become a no-op.

Logical webhook IDs have the format `whv1_<64 lowercase SHA-256 hex digits>`.
The digest input is a versioned, length-delimited encoding of `job_id`,
`run_id`, and event (for example `job.complete`). It never includes the URL,
headers, API keys, proxy credentials, or YAML. The same ID is stored as the
outbox primary key and in the receiver payload. Repeated worker submission,
startup repair, and restart replay therefore address the same row. Existing
pending, delivered, or permanently failed rows are never reset by repair.

HTTP starts only after the terminal transaction commits. Delivery is
at-least-once: a process or network failure after the receiver accepts a POST
but before Scrapeyard records success can produce another POST. Receivers
should deduplicate by `delivery_id`. Endpoint timeouts, retryable responses,
permanent 4xx responses, and shutdown cancellation never change the terminal
job or run status.

### Retry boundaries and dispatch capacity

`SCRAPEYARD_WEBHOOK_MAX_DELIVERY_ATTEMPTS` is a total-attempt limit: the first
attempt is included. Before starting an HTTP request, Scrapeyard atomically
increments `webhook_deliveries.attempts`. A pending row with `attempts >=` the
configured limit becomes failed with `attempt_exhausted` without another HTTP
call. A retryable result that consumes the last attempt becomes failed
immediately. Persisting the increment before HTTP means restart cannot reset or
silently exceed the configured request-start budget; a crash after reservation
but before the request starts can conservatively consume an attempt.

Delivery age comes only from persisted `created_at`, never process uptime. The
deadline is `created_at + SCRAPEYARD_WEBHOOK_MAX_DELIVERY_AGE_SECONDS`; the
exact boundary is closed, so `now >= deadline` becomes `age_exhausted` without
another request. Age is checked by the coordinator, immediately before attempt
reservation, after a retryable response, and before saving the next retry.
A request that started below the boundary may still record its success or
permanent response after the boundary, but it cannot schedule another retry.
A next retry time at or beyond the deadline fails immediately with
`age_exhausted`.

Successful 2xx responses are delivered. HTTP 429 and all 5xx responses are
retryable; other 4xx responses are terminal `permanent_http_response` failures.
An unsafe/non-public persisted URL is terminal `unsafe_url`. Unexpected
non-transport dispatcher failures use `non_retryable_failure`. Transport
timeouts and connection/HTTP client failures are retryable until an attempt or
age boundary is reached. Stored and logged errors use sanitized status or
exception-class descriptions and never raw headers, payloads, URLs, or
exception strings.

For 429 and 503 only, `Retry-After` accepts either non-negative integer delta
seconds or a future timezone-aware HTTP date. Malformed, negative, past, or
status-inapplicable values are ignored. An accepted value is a minimum delay:
the persisted retry uses the greater of exponential backoff and `Retry-After`.
It never authorizes a retry at or beyond the maximum delivery-age deadline.
Raw `Retry-After` data is not logged.

The dispatcher owns one coordinator task, exactly
`SCRAPEYARD_WEBHOOK_DISPATCH_CONCURRENCY` worker tasks, and a queue bounded by
`SCRAPEYARD_WEBHOOK_DISPATCH_BATCH_SIZE`. The coordinator queries only bounded
due/exhausted batches and the next pending due/age time. A future row owns no
sleeping task. Thus asyncio task count is `concurrency + 1`, independent of
outbox size, and simultaneous HTTP calls cannot exceed concurrency. A local
scheduled-ID set and the durable attempt compare-and-set prevent two workers
in this process from starting the same delivery concurrently. This is not a
cross-instance lease.

Startup logs a typed summary containing pending/delivered/failed counts,
oldest-pending age, and pending attempt range/total, then starts bounded replay.
The `WebhookOutboxStore.summarize()` inspection path exposes the same
non-secret fields; operators can also inspect `status`, `attempts`,
`failure_reason`, and terminal timestamps in `jobs.db.webhook_deliveries`.
Stable failed reason codes are:

- `attempt_exhausted`: total attempts were already or became exhausted.
- `age_exhausted`: the persisted age boundary was reached or the next retry
  could not occur before it.
- `permanent_http_response`: a non-retryable HTTP response, normally a 4xx
  other than 429.
- `unsafe_url`: public-address safety validation rejected the persisted URL.
- `non_retryable_failure`: another failure could not be retried safely.

`submit()` commits or reuses the deterministic row and only wakes the
coordinator; it never waits for the endpoint. Submission during shutdown still
leaves the durable row pending for restart. Shutdown stops the coordinator,
drains only the bounded queued/in-flight set within the worker shutdown grace,
then cancels unfinished requests. A request may have reached the receiver even
when success was not durably recorded; that row stays pending with its reserved
attempt and restart may send the same `delivery_id` again. Receivers must
therefore deduplicate. The contract remains at-least-once, not exactly-once.

### Dead-letter retention and secret scrubbing

Failed rows remain fully inspectable for
`SCRAPEYARD_WEBHOOK_FAILED_RETENTION_DAYS`; delivered rows retain their full
request data for `SCRAPEYARD_WEBHOOK_DELIVERED_RETENTION_DAYS`. The existing
periodic cleanup loop processes a deterministic bounded batch and scrubs the
URL, headers, payload, timeout, and free-form last error in place. Pending rows
are never selected by terminal retention settings. A cleanup failure is logged
and retried on a later pass without stopping workers or changing scrape state.

Scrubbing deliberately keeps a minimal tombstone: `delivery_id`, `job_id`,
`run_id`, event, terminal status/reason, attempts, and lifecycle timestamps.
Item-05 reconciliation treats that tombstone as existing durable intent. It
must not be deleted: removing the only logical job/run/event evidence would
make reconciliation recreate and redeliver an old terminal event. Scrubbed
rows cannot transition back to pending and are never selected for dispatch.

At startup Scrapeyard first repairs stale running state, then scans terminal
runs in deterministic completion/job/run order. Webhook applicability is
parsed from the `config_yaml` persisted in `jobs.db` and must match the
run's persisted configuration hash. Each repair transaction rechecks that
exact terminal/config snapshot, converges a still-current parent left as
`running`, and inserts a missing deterministic intent. A config parse or hash
mismatch fails the recovery pass rather than silently guessing. Repaired rows
are committed before webhook dispatcher startup reads and schedules pending
deliveries. Redis then connects, stale queued deliveries are reconciled, and
only then does APScheduler start. Scheduler-triggered stale-running recovery
also invokes terminal-intent repair before replacing the run.

Result data has unavoidable cross-system boundaries. `results.json` is written
on the filesystem before `results_meta.db` commits, and both precede the
terminal `jobs.db` transaction. A crash can therefore leave an orphaned result
artifact or a terminal run whose result metadata is absent. Periodic artifact
reconciliation removes only sufficiently old, unowned filesystem orphans and
reports metadata-backed failures without repairing them. Startup webhook repair reads result
metadata only as best-effort payload enrichment. If that database or row is
unavailable, it still creates the required intent with a null `result_path`
and the `job_runs.record_count` snapshot. `errors.db` is also separate:
`job_runs.error_count` and the webhook `error_count` are the best available
cross-database snapshot at terminalization, falling back to zero if the error
store is unavailable during crash handling.

Stale-running recovery itself can commit immediately before the repair pass;
a process crash in that narrow window is repaired on the next startup. A crash
after the receiver accepts HTTP but before the delivered transition produces a
duplicate on restart; a crash after attempt reservation but before HTTP can
consume an attempt. SQLite uses WAL with `synchronous=NORMAL`, so host/storage
durability still depends on SQLite, filesystem, and platform guarantees. Redis
does not store webhook intent, but Redis loss still affects scrape queue work
under the queued-reconciliation policy. SQLite transactions cannot guarantee
Redis delivery persistence, exactly-once HTTP, cross-database/filesystem
atomicity, or broad multi-instance coordination. The dispatcher has no
multi-process lease and the service retains its documented single-process
operating assumption.

## Monitoring

Monitor:

- `/health` status and dependency details
- priority intake depth from `/health` (`workers.queue_depths`); compare it with
  `active_tasks` because admitted/running work is intentionally excluded
- container restart count and OOM kills
- disk free space on `/data`
- Redis availability
- worker saturation from `/health`
- job statuses: failed and partial rates
- webhook outbox failures
- scrape error types and target-domain failure concentration

Alert on low disk space before SQLite or result writes fail.

## Preflight Checks

Run these before promoting a new deployment:

```bash
curl -fsS http://127.0.0.1:8420/health
```

For deployments where Eyebox reaches Scrapeyard through the Docker host
gateway, verify the same path from a container:

```bash
docker run --rm --add-host=host.docker.internal:host-gateway curlimages/curl \
  -fsS http://host.docker.internal:8420/health
```

Unauthenticated non-health requests should fail when `SCRAPEYARD_API_KEYS` is
set:

```bash
curl -i http://127.0.0.1:8420/jobs
```

Authenticated requests should work from Eyebox's network path:

```bash
curl -fsS \
  -H "X-API-Key: $SCRAPEYARD_API_KEY" \
  http://127.0.0.1:8420/jobs
```

Private target URLs should be rejected by config validation:

```bash
cat > /tmp/private-target.yaml <<'YAML'
project: preflight
name: private-target
target:
  url: http://127.0.0.1/
  selectors:
    title: h1
YAML

curl -i \
  -H "X-API-Key: $SCRAPEYARD_API_KEY" \
  -H "Content-Type: application/x-yaml" \
  --data-binary @/tmp/private-target.yaml \
  http://127.0.0.1:8420/scrape
```

The expected response is `422` with an unsafe URL validation error.

## Go/No-Go Checklist

- `SCRAPEYARD_API_KEYS` is set and known only to Eyebox and operators.
- Scrapeyard HTTP is reachable only from Eyebox and internal monitoring.
- Redis is not exposed outside the private runtime network.
- Egress policy blocks metadata and private/internal networks.
- `/data` is persistent and has disk alerts.
- Database and result-artifact restore has been tested.
- Full test suite passes in CI.
- Staging has run representative Eyebox scrapes, unreachable targets, bad
  configs, and a restart during queued/running work.
