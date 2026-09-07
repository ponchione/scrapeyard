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

## Metadata layout decision (TD-14)

**2026-09-07: retain the three metadata databases; close TD-14 as an accepted
architecture tradeoff.** The review proposed consolidation, without a reproduced
correctness defect or a consumer requirement for it. A local comparison of the
existing stores found no clear performance benefit from file consolidation.
There is no planned consolidation migration for the supported single-process
deployment.

This accepts the existing publication gap: result metadata commits before the
ownership-checked transaction that finalizes the run, parent, and webhook intent.
That gap has not become atomic. Cancellation and recovery still need to handle
unowned results. Deletion still reserves the job, verifies Redis quiescence,
cleans errors and optional result artifacts, and finalizes the reservation;
retained results remain readable after their job is deleted. Backups still
quiesce writers and snapshot all three databases plus artifacts together.
See [the lifecycle contract](JOB_LIFECYCLE.md#failure-and-consistency-limits).

A combined result/finalization transaction could remove that particular metadata
gap. Merely placing the tables in one file cannot: it leaves the current stores'
separate commits intact. Even a combined transaction would retain filesystem and
Redis reconciliation, deletion reservations, and quiesced backups. Changing
those paths and migrating existing ledgers, encrypted values, and retained
records has a concrete implementation cost; the present review establishes no
benefit sufficient to justify that migration.

### Measurement and reproduction

Run from the checkout with the locked Poetry environment:

```bash
poetry run python scripts/benchmark_metadata.py --repeats 5
```

The benchmark creates and removes only temporary databases and artifact
directories. It uses the production migrations, encrypted job/run/outbox writes,
result-file writer, ownership-checked finalization, and authenticated `/jobs`
route. It never starts Redis, scraping, a scheduler, or webhook delivery. Its
scratch consolidation is not a legacy migration and must not be used as one.

Each trial seeds 1,000 terminal parent jobs without run history, then executes
eight batches of 12 new runs and 80 `/jobs?limit=100` reads: 96 runs and 640 reads
per layout. Four writers and 16 readers match the concurrency in the existing
release load harness. Each run records one recovered fetch error, writes a
one-record result artifact, and finalizes with a webhook intent. Fetch/browser
delays are omitted to concentrate storage pressure; this is a storage experiment,
not the full release workload or an Eyebox operating-envelope qualification.
API timing uses in-process ASGI requests, excluding the client semaphore wait.
The benchmark raises its scratch API request limit to admit the measured reads.

Five trials per layout, with rotating layout order, ran against application
commit `ca4cdac7246abf760b9d3f1690c1a86b41705202`, Python 3.12.3 and SQLite
3.45.1 on the local Linux host. [Raw observations](benchmarks/metadata-2026-09-07.jsonl)
include environment details and every trial. Values below are medians across
trials; API ranges show the minimum and maximum trial p95.

| Layout | Runs/s | API p95 ms (range) | Writer connection wait p95 ms | Writer connection held p95 ms |
| --- | ---: | ---: | ---: | ---: |
| Three files, three connections (current) | 43.38 | 48.67 (47.31–55.30) | 44.28 | 0.657 |
| One file, one shared connection/lock | 42.10 | 46.83 (45.92–50.19) | 44.40 | 0.585 |
| One file, three connections/locks | 41.91 | 53.71 (48.69–54.14) | 44.65 | 1.263 |

Connection wait measures acquisition of the application's connection lock;
connection-held time includes SQL, commits, and any SQLite writer wait. These
are not isolated SQLite lock measurements. All layouts retain the current
transaction boundaries. Overlapping timings do not establish a general winner,
and this experiment does not measure the possible benefit of combining commits,
large artifacts, long run histories, cleanup contention, or production traffic.
It supports keeping the current layout without a speculative performance claim.

Every trial checks row counts, database integrity, terminal parent status,
decryption of saved job configurations, and all result artifacts. Existing tests
exercise rollback of terminal state/webhook intent, cancellation, resumable
cross-store deletion and retention, recovery, and backup/restore with results
retained after job deletion. Those contracts remain the acceptance criteria for
the retained design.

Validation for this decision: `poetry run pytest -W error -q` passed 2,081 tests
with 16 skipped and 88.50% coverage, including the unit and integration lifecycle
and backup/restore checks. Ruff (`src tests scripts`), strict mypy (95 source
files), and `poetry check --lock` also passed. Live Redis/browser and full release
qualification were not run for this decision.

Reopen the architecture decision when a concrete consistency requirement,
measured maintenance cost, or representative persistence bottleneck warrants it.
A future consolidation must expose a combined ownership-checked commit, prove
populated and interrupted migration safety, and requalify lifecycle and backup
behavior before replacing the existing stores.

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
