# Runtime Topology and Scaling Boundary

## Supported topology today

One Scrapeyard deployment is exactly one application process, one embedded
arq worker pool, one APScheduler instance, and one cleanup/webhook coordinator.
Increasing `SCRAPEYARD_WORKERS_MAX_CONCURRENT` or
`SCRAPEYARD_WORKERS_MAX_BROWSERS` adds bounded work inside that process; it
does not create application replicas.

The process acquires an exclusive POSIX advisory lock at
`SCRAPEYARD_DB_DIR/.scrapeyard-instance.lock` before opening SQLite or starting
Redis-backed workers. A second process sharing that state directory fails
immediately with the current owner PID/host and remediation. The lock file is
mode `0600`, contains no Redis credentials, and is intentionally not unlinked.
Normal shutdown clears and releases it; the kernel releases it after a crash,
so a stale file or stale owner metadata does not block restart. The shared
volume must provide correct Linux `flock(2)` semantics.

Scrapeyard also rejects `WEB_CONCURRENCY`, Uvicorn, or Gunicorn worker settings
other than `1`. The image entrypoint validates the command before Uvicorn
starts, and lifespan validates again for direct source launches or overridden
entrypoints. The Dockerfile and Compose service both specify `--workers 1`, and
Compose declares one replica. Do not use `docker compose up --scale`, multiple
pods, Gunicorn process workers, or Uvicorn `--workers` above one.

An intentionally separate deployment must use a distinct set of all state
identities: SQLite directory, result/adaptive/log directories, Redis logical
database, and queue name. Sharing only some of those values is unsupported.

## State ownership

| Scope | State and responsibility |
| --- | --- |
| Process-local | FastAPI lifespan and health uptime/cache; Prometheus process metrics; embedded arq runner; browser limiter; APScheduler runtime; cleanup and queued-reconciliation loops; webhook coordinator; circuit-breaker state; and the local rate limiter when Redis sharing is disabled. |
| Redis-shared | arq job payloads/results, priority admission/delivery state, queue cancellation state, and the domain rate limiter when `SCRAPEYARD_DOMAIN_RATE_LIMIT_SHARED=true`. The limiter uses Redis server time and clamps backward movement to one configured interval. Redis does not make the scheduler, SQLite, files, or process counters distributed. |
| SQLite-shared | Job/run/config/idempotency state and webhook intents in `jobs.db`; error records in `errors.db`; result metadata in `results_meta.db`; migration ledgers and transaction boundaries for each database. |
| Filesystem-shared | Result JSON/screenshots/debug artifacts, adaptive selector state, logs, and the single-instance lock. Filesystem writes are not atomic with SQLite or Redis transactions. |

This division is why adding HTTP replicas is unsafe even though queue delivery
uses Redis: every replica would otherwise start another scheduler, recovery
pass, cleanup loop, webhook coordinator, and embedded worker pool while
maintaining divergent process-local health and resilience state.

## Future distributed topology

Horizontal scaling requires a deliberate service split, not removal of the
lock alone:

1. Make the API enqueue-only and stateless, with no embedded scheduler,
   scraper workers, cleanup loop, or process-local coordination state.
2. Run arq workers as a separate deployment with explicit queue leases,
   idempotent ownership transitions, shared concurrency/rate controls, and
   worker-specific health/metrics.
3. Run APScheduler and maintenance/reconciliation as separately elected
   singletons, or replace them with an external scheduler that provides leader
   election and durable triggers.
4. Move the three SQLite stores to a transactional network database, including
   migration coordination and row-level ownership/lease semantics. Preserve
   terminal webhook intent atomicity in that shared store.
5. Move local results and adaptive artifacts to shared object storage with
   atomic publication, immutable keys, retention coordination, and explicit
   consistency handling.
6. Separate webhook delivery into leased workers, and aggregate health,
   metrics, circuit-breaker, and capacity state across processes.
7. Add multi-replica failure, duplicate-delivery, leader-failover, migration,
   load, and disaster-recovery qualification before declaring that topology
   supported.

Until those changes land together, the startup guard is a correctness boundary
and must not be disabled.

## Intentional operating limitations

- Horizontal scaling is unsupported: one deployment means one process and one
  replica. Increasing only the in-process worker/browser limits is the supported
  capacity control.
- APScheduler does not backfill cron fires missed while Scrapeyard is stopped.
  On recovery, the next future cron occurrence is eligible; use an explicit
  manual run when operators decide missed work must be replayed.
- Webhook delivery is at-least-once. A crash after a receiver accepts a request
  but before the delivered transition can repeat it, so receivers must
  deduplicate by the stable delivery/event identifiers.
