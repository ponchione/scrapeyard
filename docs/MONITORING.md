# Monitoring and Readiness

Scrapeyard exposes Prometheus text metrics at `GET /metrics`. The endpoint and
the detailed `GET /health/ready` probe require a credential with the
`health-detail` scope. `GET /health` and `GET /health/live` remain public,
constant-cost process-liveness checks and intentionally reveal no dependency
state.

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
by default. Queue snapshots use a fixed six Redis commands (depth and first
score for each of three priorities); webhook summaries use aggregate SQLite
queries. Scrapes never scan job IDs, URLs, projects, or all queue members.

- API request count and latency use method, route template, and status class.
- Queue depth and oldest waiting age use only `high`, `normal`, and `low`.
- Active jobs, targets, and browser targets are compared with process capacity.
- Run and target count/latency use bounded status, trigger, and fetcher labels.
- Retry, rate-limit wait, extracted-record, and serialized-byte totals show
  work amplification and output pressure.
- Durable webhook status counts and oldest-pending age expose retry backlog.
- Cleanup pass/item/removed-byte totals and scheduler, cleanup, and webhook
  last-success timestamps expose stalled maintenance.
- Background-task gauges report the embedded worker, scheduler, cleanup loop,
  and webhook dispatcher. Result-filesystem free bytes expose disk pressure.

Raw URL, project, job ID, run ID, delivery ID, caller, and credential labels are
never emitted. This keeps time-series cardinality fixed as tenants and jobs
grow.

## Readiness semantics

`/health/ready` applies an explicit short timeout to Redis, queue, every SQLite
database (`jobs.db`, `errors.db`, and `results_meta.db`), disk, and a one-byte
atomic create/read/remove operation in the result directory. It also fails when
the worker runner, APScheduler, cleanup task, or webhook coordinator/worker set
has stopped or failed. Exhausted worker capacity returns a degraded `200`;
insufficient free disk or any other failed required dependency/background task
returns `503`. Liveness never performs these operations.

Tune the bounded probes with:

- `SCRAPEYARD_HEALTH_PROBE_TIMEOUT_SECONDS` (default `2`)
- `SCRAPEYARD_METRICS_REFRESH_INTERVAL_SECONDS` (default `5`)
- `SCRAPEYARD_HEALTH_DISK_FREE_MIN_MB` (default `100`)

## Suggested alerts

- Page when `scrapeyard_background_task_up == 0` for two minutes or readiness
  returns `503` for two consecutive probe intervals.
- Alert on sustained `active_work{kind="jobs"} / work_capacity{kind="jobs"}`
  above 0.9, or browsers above 0.9 with growing queue age.
- Alert when any priority's oldest queue age exceeds the run service objective,
  even if depth is low.
- Alert when webhook pending count or oldest-pending age grows across multiple
  retry windows.
- Alert before `scrapeyard_result_storage_free_bytes` reaches the configured
  readiness floor; keep a separate host-volume inode alert.
- Alert when cleanup or scheduler last-success age exceeds twice its expected
  interval. A zero timestamp means that subsystem has not yet succeeded.
- Track run/target failure ratios, retry amplification, API tail latency, output
  bytes, and cleanup failures as dashboard trends rather than per-ID series.

Metrics are process-local by design because the current deployment supports one
application process. A future multi-process topology must use a shared metrics
aggregation design along with the runtime split described in the architecture
notes.
