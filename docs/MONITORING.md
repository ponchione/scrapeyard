# Monitoring and Readiness

Scrapeyard exposes Prometheus text metrics at `GET /metrics`. The endpoint and
the detailed `GET /health/ready` probe require a credential with the
`health-detail` scope. `GET /health` and `GET /health/live` remain public,
constant-cost process-liveness checks and intentionally reveal no dependency
state.

Production must define a dedicated credential with exactly that one scope, no
project restriction, and place its secret in
`SCRAPEYARD_HEALTH_PROBE_API_KEY`. Startup rejects a missing, unknown,
over-privileged, or project-restricted health credential. Compose sends this
credential to `/health/ready` from the container healthcheck; it is read from
the environment inside the process and never printed or placed in the probe's
command-line arguments. Do not reuse an operator, submitter, or deletion key.

```yaml
scrape_configs:
  - job_name: scrapeyard
    metrics_path: /metrics
    # Point this target at a private monitoring proxy that maps the bearer
    # credential to Scrapeyard's X-API-Key request header.
    authorization:
      credentials_file: /run/secrets/scrapeyard_metrics_key
    static_configs:
      - targets: ["scrapeyard-monitor-proxy:8421"]
```

Prometheus sends the standard `Authorization` header, while Scrapeyard accepts
`X-API-Key`. Use a private proxy that performs that fixed header mapping, or an
exporter agent that supports custom request headers. Never place the metrics
credential in a checked-in configuration file.

## What is measured

The exporter has a process-local registry and a five-second durable-gauge cache
by default. Queue snapshots use one fixed-cost Redis script that reads the depth,
oldest member, and enqueue timestamp for each of three priorities; webhook
summaries use aggregate SQLite queries. Scrapes never scan job IDs, URLs,
projects, or all queue members.

- API request count and latency use method, route template, and status class.
- Queue depth, oldest waiting age, and enqueue-clock rollback offset use only
  `high`, `normal`, and `low`.
- Active jobs, targets, browser targets, bounded run threads, and result-response
  threads are compared with process capacity. Lingering threads separately show work still
  consuming a slot after its owning run reached a terminal deadline.
- Run and target count/latency use bounded status, trigger, and fetcher labels.
- Retry, rate-limit wait, extracted-record, and serialized-byte totals show
  work amplification and output pressure.
- Durable webhook status counts and oldest-pending age expose retry backlog.
- Cleanup pass/item/removed-byte totals, artifact finding counts, durable
  history phase failures, exact eligible backlog count/oldest age, and
  scheduler/cleanup/webhook/reconciliation last-success timestamps expose
  stalled maintenance and retained-data integrity.
- Background-task gauges report the embedded worker, scheduler, cleanup loop,
  webhook dispatcher, queued-delivery reconciler, and stale-running reconciler.
  Scheduler readiness also includes durable per-schedule callback failures;
  one healthy schedule cannot mask another schedule that fails before enqueue.
  Reconciliation pass counters distinguish success from failure. Result-filesystem
  free bytes expose disk pressure.

Raw URL, project, job ID, run ID, delivery ID, caller, and credential labels are
never emitted. This keeps time-series cardinality fixed as tenants and jobs
grow.

## Readiness semantics

`/health/ready` applies an explicit short timeout to Redis, queue, and a newly
opened SQLite `mode=rw` connection for every database (`jobs.db`, `errors.db`,
and `results_meta.db`). Each database probe runs `PRAGMA quick_check(1)`, then
performs a schema write inside `BEGIN IMMEDIATE` and rolls it back. This proves
actual write access without retaining a table or row. On Linux, it also rejects
any cached descriptor that procfs identifies as a deleted database WAL or SHM
sidecar. Every database-path descriptor and lock operation stays inside
SQLite's connection lifecycle; readiness must never use raw file opens against
a live database path. Opening a fresh connection ensures a cached process
connection cannot mask a missing, inaccessible, read-only, or damaged database
path. It also fails when
the worker runner, APScheduler, cleanup task, webhook coordinator/worker set, or
either interval-aware reconciliation service has stopped or gone too long without
a successful pass. Optional project summaries use the same configured timeout,
retain the last successful cache on refresh failure, and make readiness return
`503` with an explicit detail when a requested refresh fails. Exhausted worker
capacity returns a degraded `200`;
insufficient free disk or any other failed required dependency/background task
returns `503`. Liveness never performs these operations.

SQLite failures identify the database, bounded operation (`open_readwrite`,
`quick_check(1)`, `begin_immediate`, `write_test`, `rollback`, `close`, or
`descriptor_check`), exception class, and SQLite error name when available.
Filesystem paths, SQL text, exception messages, configuration, and credentials
are not returned. For example, a missing file reports the operation and
`OperationalError (SQLITE_CANTOPEN)` without exposing its path.
Redis, queue-depth, result-storage, and disk failures follow the same boundary:
readiness returns a stable probe category and exception class, never raw adapter
text, a DSN, credentials, or the configured filesystem path.

The raw descriptor prohibition is a correctness boundary on Unix. Closing any
separately opened descriptor for a database file can cancel all process-local
POSIX advisory locks on that inode, including locks owned by cached SQLite
connections. An independent reader may then act as the apparent final
connection and unlink WAL/SHM paths underneath the cached connection. SQLite
3.51.3 adds defense in depth, but callers must still use SQLite's APIs for all
live database access.

The quick and full release qualifications prove this contract destructively:
Redis is stopped, a SQLite database and result storage are made inaccessible,
the disk floor is raised above available space, and a required cleanup task is
forced to fail. Each condition must make both `/health/ready` and Docker health
unhealthy; restoring the dependency or task must return both to healthy.

The two synchronous result-filesystem probes are single-flight and run in a
dedicated two-thread executor. A timed-out probe continues to occupy only its
own one-probe slot, is logged as still running, and is reused by concurrent or
later readiness requests until the underlying filesystem call actually exits.
Monitoring polls therefore cannot queue unbounded abandoned work or starve the
default executor used by request and worker operations.

Tune the bounded probes with:

- `SCRAPEYARD_HEALTH_PROBE_TIMEOUT_SECONDS` (default `2`)
- `SCRAPEYARD_METRICS_REFRESH_INTERVAL_SECONDS` (default `5`)
- `SCRAPEYARD_HEALTH_DISK_FREE_MIN_MB` (default `100`)

## Suggested alerts

- Page when `scrapeyard_background_task_up == 0` for two minutes or readiness
  returns `503` for two consecutive probe intervals.
- Alert on sustained `active_work{kind="jobs"} / work_capacity{kind="jobs"}`
  above 0.9, or browsers above 0.9 with growing queue age.
- Alert when `scrapeyard_active_work{kind="lingering_run_threads"}` remains
  nonzero; sustained saturation of run-thread capacity indicates blocking
  selector, validation, or resolver work that outlived its run deadline.
- Alert when any priority's oldest queue age exceeds the run service objective,
  even if depth is low.
- Page when `scrapeyard_queue_clock_rollback_offset_seconds` is nonzero; intake
  remains admissible, but the signal indicates that a dependency clock stepped
  behind an already-recorded enqueue timestamp.
- Alert when webhook pending count or oldest-pending age grows across multiple
  retry windows.
- Alert before `scrapeyard_result_storage_free_bytes` reaches the configured
  readiness floor; keep a separate host-volume inode alert.
- Alert when cleanup or scheduler last-success age exceeds twice its expected
  interval. A zero timestamp means that subsystem has not yet succeeded.
- Alert when `scrapeyard_cleanup_eligible_items` or
  `scrapeyard_cleanup_oldest_eligible_age_seconds` rises across maintenance
  cycles for any category; successful bounded transactions do not by
  themselves prove that retention is keeping up.
- Page on scheduler readiness details beginning `scheduled callback failures`.
  Inspect the affected job's `schedule_failure_code`, failure timestamp,
  consecutive count, and synthetic failed run. Codes are bounded
  classifications; raw exception or secret text is intentionally unavailable.
- Alert on any increase in
  `scrapeyard_cleanup_artifact_findings_total{kind=~"missing|corrupt|unreadable|unsafe"}`.
  This signal means retained result metadata no longer validates against its
  artifact. Restore the artifact, intentionally delete its metadata, or record
  an explicit decision to accept the loss; cleanup does not make that decision.
- Alert on any increase in `scrapeyard_cleanup_history_failures_total`; the
  bounded `phase` label identifies whether selection, cross-database error
  deletion, or final jobs/run compaction needs investigation. Persisted
  deletion reservations are retried automatically.
- Alert on increases in
  `scrapeyard_reconciliation_passes_total{status="failure"}` and on queued or
  running reconciliation last-success age beyond two configured intervals.
- Track run/target failure ratios, retry amplification, API tail latency, output
  bytes, and cleanup failures as dashboard trends rather than per-ID series.

Metrics are process-local by design because the current deployment supports one
application process. A future multi-process topology must use a shared metrics
aggregation design along with the runtime split described in the architecture
notes.
