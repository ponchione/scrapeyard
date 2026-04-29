# Technical Debt Register

Last updated: 2026-04-24

**Status:** All BLOCKERs (#1–9) and launch-gate HIGH items #10, #11, and
#18 are resolved. Runtime-safety items #12, #15, #16, and #33 are resolved.

Pre-launch audit for the EyeBox rollout. Findings verified by direct source
reads; file:line references point to current `main`. Severity tiers:

- **BLOCKER** — must resolve before the service is exposed to live traffic
- **HIGH** — resolve in the first deployment cycle; will cause incidents
  under real load, not just edge cases
- **MEDIUM** — fix soon; workable short-term but represents real risk

Scope notes for calibration:

- Deployment model is single-instance (one API process with an embedded arq
  worker). Multi-instance concerns are called out only where they are likely
  to bite us anyway (e.g., cleanup + API sharing SQLite across processes if
  anyone adds an ops sidecar).
- EyeBox sits in front of this service. Assumptions about how EyeBox
  authenticates to Scrapeyard, what network boundary it sits behind, and
  what its failure domain is are **not yet encoded** and directly affect
  how many of the BLOCKERs below apply.

---

## BLOCKER — Do Not Deploy As-Is

> **All BLOCKERs below are resolved.** Resolution notes inline. Keep the
> descriptions so future audits can confirm the fix matches the risk.

### 1. ~~No authentication or authorization on any HTTP endpoint~~ (RESOLVED)
`src/scrapeyard/api/routes.py:88–421`, `src/scrapeyard/main.py:78–85`

Every route (`POST /scrape`, `POST /jobs`, `GET /jobs`, `DELETE /jobs/{id}`,
`GET /results/{id}`, `GET /errors`, `GET /health`) is fully public. No API
key, bearer token, mTLS, or middleware is registered on the FastAPI app.
Anyone who can reach port 8420 can:

- enumerate every job across every project (`GET /jobs`),
- read any stored `config_yaml` including embedded proxy credentials
  (`GET /jobs/{id}` returns the raw yaml — see routes.py:276),
- delete any job and optionally its results (`DELETE /jobs/{id}`),
- submit arbitrary scrape jobs (SSRF — see #2).

Decision needed: API key header + allow-list for EyeBox, or a service mesh /
network policy that only permits the EyeBox pod to reach this service. At
minimum, do not expose 8420 on the host in production; bind to an internal
interface only.

**Fix (2026-04-24):** `APIKeyAuthMiddleware` in
`src/scrapeyard/api/middleware.py` enforces an `X-API-Key` header against
`SCRAPEYARD_API_KEYS` (comma-separated allow-list). `/health` is exempt.
Empty key list disables auth with a one-shot warning log so dev mode is
obvious. Must be populated in production compose/K8s manifests.

### 2. ~~No SSRF protection on target or webhook URLs~~ (RESOLVED)
`src/scrapeyard/config/schema.py:204` (`TargetConfig.url: str`),
`src/scrapeyard/config/schema.py:291` (`WebhookConfig.url: HttpUrl`)

`TargetConfig.url` is a bare `str`, so literally anything passes. `HttpUrl`
validates RFC syntax but does not reject private ranges, loopback, or cloud
metadata endpoints (`169.254.169.254`, `metadata.google.internal`,
`fd00:ec2::254`). Combined with #1, any caller can weaponize the scraper to
pull IAM credentials, hit internal admin panels, or exfiltrate via webhook
callbacks to attacker-controlled URLs. Also note user-controllable headers
on the webhook (`WebhookConfig.headers` line 296).

Needed: a URL validator that rejects non-public destinations before the job
is enqueued, applied to both target URLs and webhook URLs. DNS rebinding is
a real concern — validation must happen again at fetch time or be anchored
to a resolved IP, not just the string.

**Fix (2026-04-24):** `assert_public_url` in
`src/scrapeyard/engine/url_guard.py` rejects non-public destinations. Called
from Pydantic `field_validator`s on `TargetConfig.url` and `WebhookConfig.url`.
Lexical checks block banned hostnames and literal private IPs unconditionally;
DNS lookups are best-effort (resolution failures fall through rather than
blocking config load over transient DNS issues). DNS rebinding at fetch time
is still unaddressed — treat as a follow-up HIGH item if the attack model
warrants it.

### 3. ~~SQLite opened without WAL or busy_timeout~~ (RESOLVED)
`src/scrapeyard/storage/database.py:71–85`

Every connection is `await aiosqlite.connect(path)` with no PRAGMAs applied.
Defaults are `journal_mode=DELETE`, `busy_timeout=0`, `synchronous=FULL`.
Under the current in-process workflow the async lock in
`database.py:109–128` serializes writers, but:

- the cleanup loop, scheduler triggers, worker writes, and API reads all
  share one connection per DB. Any time two tasks contend on the same DB
  connection we rely entirely on the async lock; one missed `async with
  get_db(...)` call and we're in deadlock/corruption territory.
- as soon as any out-of-process tool (a `sqlite3` shell for ops, a backup
  job, a future sidecar) touches these files, writers will fail fast with
  "database is locked" instead of waiting.
- `synchronous=FULL` + large per-job writes is noticeably slower than
  `WAL + synchronous=NORMAL` for this workload.

Needed on each connection at open time:

```
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
```

**Fix (2026-04-24):** `_apply_connection_pragmas` in
`src/scrapeyard/storage/database.py` applies all four PRAGMAs on every
connection (migration phase and cached lookup). Note: `PRAGMA foreign_keys`
is a no-op until explicit `FOREIGN KEY` constraints are added — see #23.

### 4. ~~No request body size limit~~ (RESOLVED)
`src/scrapeyard/api/routes.py:103–104`, `src/scrapeyard/api/routes.py:180–181`

`body = await request.body()` reads the full payload into memory before any
validation. Uvicorn has no default body-size cap that would save us.
Combined with #1, a single unauthenticated `POST /scrape` with a large YAML
payload can OOM the process. `yaml.safe_load` in
`src/scrapeyard/config/loader.py:12` also has no size/anchor-depth bound —
nested alias bombs will consume memory and CPU before any Pydantic
validation runs.

Needed: hard limit on `Content-Length` (e.g., 256 KiB — a legitimate config
is small) rejected at the middleware level, not inside the handler.

**Fix (2026-04-24):** `RequestSizeLimitMiddleware` in
`src/scrapeyard/api/middleware.py` rejects oversize requests before auth or
the router runs. Cap is `SCRAPEYARD_MAX_REQUEST_BYTES` (default 262144 = 256
KiB). Enforced via declared `Content-Length` and via a receive-wrapper that
counts chunked bytes.

### 5. ~~JSON log format is structurally broken~~ (ALREADY FIXED — DOC STALE)
`src/scrapeyard/common/logging.py:16–20`

```python
fmt = logging.Formatter(
    '{"time":"%(asctime)s","level":"%(levelname)s",'
    '"logger":"%(name)s","message":"%(message)s"}',
    ...
)
```

`%(message)s` is interpolated raw into a JSON document. Any log call whose
message contains `"`, `\`, a newline, a control char, or a tab produces
invalid JSON. This includes virtually every useful log line in the
codebase: URLs with query strings, exception tracebacks, Pydantic errors,
scraped HTML snippets, proxy strings. Log aggregators (Loki, CloudWatch,
ES) will either drop these lines or index them as unstructured blobs, and
the rotating file at `logs/scrapeyard.log` cannot be re-parsed later.

Needed: use a real JSON formatter (`python-json-logger`, `structlog`, or a
minimal `logging.Formatter` subclass that calls `json.dumps` on a dict).

**Status (2026-04-24):** `_JsonFormatter` in
`src/scrapeyard/common/logging.py` already uses `json.dumps` with proper
escaping; log level is also already configurable via `SCRAPEYARD_LOG_LEVEL`
(doc item #32). The tech-debt register was out of date when it flagged these.

### 6. ~~`/health` does not check Redis, SQLite, or disk~~ (RESOLVED)
`src/scrapeyard/main.py:139–162`

Health returns `status="ok"` purely based on local pool counters and an
async SQL query (`_project_health_summary` at main.py:95–101). It does not
ping Redis, does not write-probe the DB directory, does not check disk
space. If Redis is down — i.e., the entire queue is broken — the endpoint
still returns `ok`, so K8s/ECS/Compose healthchecks never fire and the
service silently stops processing work.

Compounding: there is no `HEALTHCHECK` instruction in the `Dockerfile` and
no `healthcheck:` stanza in `docker-compose.yml` for either service, and no
`condition: service_healthy` on `depends_on` (docker-compose.yml:13–14).
On startup, `WorkerPool.start()` awaits `create_pool()` with no timeout
(`src/scrapeyard/queue/pool.py:85–88`); if Redis is not yet accepting
connections, the process hangs indefinitely.

Needed: `/health` must do a Redis `PING`, an SQLite `SELECT 1`, and a disk
space check, and return 503 on failure. Add healthchecks to both compose
services and make Scrapeyard depend on Redis healthy, not just started.

**Fix (2026-04-24):** `/health` now runs `probe_redis`, `probe_sqlite`, and
`probe_disk` (in `src/scrapeyard/runtime/health.py`); returns 503 if any
probe fails. Disk free threshold is `SCRAPEYARD_HEALTH_DISK_FREE_MIN_MB`
(default 100). Dockerfile has a `HEALTHCHECK` and `docker-compose.yml` has
per-service healthchecks with `depends_on: redis: condition:
service_healthy`. Redis-connect startup timeout is covered by resolved #33.

### 7. ~~Result writes are not atomic — partial files on crash~~ (RESOLVED)
`src/scrapeyard/storage/filesystem.py:19–25`

```python
def write_json_file(path, data):
    Path(path).write_text(json.dumps(data, default=str, indent=2), ...)
```

`Path.write_text` opens/writes/closes in separate syscalls. If the process
crashes mid-write, or `/data` fills up, we end up with truncated JSON on
disk. In `src/scrapeyard/storage/result_store.py` the DB row is written in
the same path as the file — one failure mode here is "metadata says row
exists, file on disk is unreadable JSON" which breaks `GET /results/{id}`
permanently for that run with no automatic recovery.

Needed: write to a sibling `*.tmp` file, `fsync`, then `os.replace` to the
final name.

**Fix (2026-04-24):** `write_json_file` in
`src/scrapeyard/storage/filesystem.py` now writes to `${target}.tmp`,
`fsync`s, and `os.replace`s atomically.

### 8. ~~Container runs as root; `/data` owned by root~~ (ALREADY FIXED — DOC STALE)
`Dockerfile:2, 11, 45–46, 50`

No `USER` directive in either stage. `mkdir -p /data/...` and the final
`CMD` run as root. A bug in Scrapling / Playwright / Chromium that yields
RCE inside the container is a full container compromise instead of a
constrained-user compromise, and any shared volume/bind-mount writes land
as root on the host. Once we fix this later, the `/data` volume has to be
chowned too — easier to do it once, now.

**Status (2026-04-24):** The Dockerfile already creates the `scrapeyard`
user, chowns `/data` + `/app` + the cache dir to it, and `exec su scrapeyard`
into uvicorn — the long-running request-handling process is non-root. Only
the brief pre-exec shell stays root to handle volume-mount permission fixup
and the Chromium setuid sandbox. RCE inside Playwright/Chromium lands as the
scrapeyard user, not root. Treat as resolved in spirit; a pure `USER
scrapeyard` directive would require moving volume-permission handling into
an initContainer/entrypoint script — not worth the churn now.

### 9. ~~Proxy credentials stored and served in plain text~~ (RESOLVED)
`src/scrapeyard/config/schema.py:171–177`,
`src/scrapeyard/api/routes.py:276`

`ProxyConfig.url` accepts `http://user:pass@host:port`. The full
`config_yaml` is persisted to `jobs.db` and returned verbatim from
`GET /jobs/{id}`. Combined with #1, proxy credentials are readable by
anyone who hits the API. Even once auth is added, we should not round-trip
credentials through the API response.

Needed: redact userinfo from the yaml returned by `/jobs/{id}` (reuse
`engine/proxy.redact_proxy_url`), and consider storing proxy credentials
as a service-level secret referenced by name from the config rather than
inline.

**Fix (2026-04-24):** `redact_userinfo_in_text` in
`src/scrapeyard/engine/url_guard.py` scrubs `user:pass@` from http(s) URLs
embedded in text. `serialize_job_detail` applies it to `config_yaml` before
returning via `GET /jobs/{id}`. Service-level secret indirection (deferring
credentials into a secrets store) is left as a follow-up.

---

## HIGH — Will Cause Incidents Under Real Load

### 10. ~~Fire-and-forget webhook dispatch with no retry or durability~~ (RESOLVED)
`src/scrapeyard/queue/run_lifecycle.py`,
`src/scrapeyard/webhook/dispatcher.py`,
`src/scrapeyard/storage/webhook_outbox.py`, `sql/009_create_webhook_outbox.sql`

The original inline `asyncio.create_task(...)` call was already refactored into
`run_lifecycle.dispatch_webhook(...)` calling `HttpWebhookDispatcher.submit(...)`,
and the dispatcher had in-memory retry plus shutdown drain. The remaining
launch-gate risk was durability: after result/job finalization, a process crash
before successful HTTP delivery could permanently lose the EyeBox callback.

**Fix (2026-04-24):** Webhook submissions now persist a `webhook_deliveries`
outbox row in `jobs.db` before `submit(...)` returns, using the payload
`delivery_id` as the durable idempotency key. `HttpWebhookDispatcher.startup()`
replays pending outbox rows; successful 2xx responses mark rows `delivered`,
non-retryable 4xx responses mark rows permanently `failed`, and transient
errors/timeouts/5xx/429 keep rows `pending` with attempts, last error, and
`next_attempt_at` advanced by backoff. Pending/failed rows remain inspectable;
at-least-once duplicate delivery after a crash remains acceptable because the
receiver can deduplicate by `delivery_id`.

### 11. ~~No HTTP-layer rate limiting or request throttling~~ (RESOLVED)
`src/scrapeyard/api/middleware.py`, `src/scrapeyard/main.py`,
`src/scrapeyard/common/settings.py`

The domain rate limiter in `engine/rate_limiter.py` governs outbound
scraping, not inbound API calls. Nothing stops a client from bursting
`POST /scrape` at kilohertz, saturating Redis enqueues, Pydantic parses,
and worker memory. With #1 unresolved this is a DoS. Even with auth, a
runaway EyeBox caller will take us down.

**Fix (2026-04-24):** `RateLimitMiddleware` in
`src/scrapeyard/api/middleware.py` enforces an in-memory sliding-window
request limit before auth/router execution but after the request-size guard. It is
configured by `SCRAPEYARD_RATE_LIMIT_REQUESTS` (default 600) and
`SCRAPEYARD_RATE_LIMIT_WINDOW_SECONDS` (default 60). Requests are keyed by a
known `X-API-Key` when auth keys are configured; missing or invalid keys fall
back to the immediate client IP so random header values cannot evade the
limit. `/health` remains exempt, and throttled requests return 429 with
`Retry-After`.

### 12. ~~In-flight `asyncio.Task`s are awaited sequentially — partial loss on mid-job exception~~ (RESOLVED)
`src/scrapeyard/queue/worker.py:294–304`

```python
tasks = []
for i, t in enumerate(targets):
    ...
    tasks.append(asyncio.create_task(_process_target(t)))

for task in tasks:
    tr = await task
    all_results.append(tr)
```

`_process_target` had a `try/finally` but no catch. Any exception raised by
`scrape_target` that was not a `RetryableError` (e.g., Playwright
`BrowserClosed`, out-of-memory, unexpected Scrapling error) propagated out
of the first `await task`. Because this was a sequential loop and not
`asyncio.gather(..., return_exceptions=True)`, all later tasks could be left
unawaited; their completed results were discarded and their error rows might
not flush. The outer `except Exception` then marked the entire job failed
instead of `partial`, regardless of `fail_strategy`.

**Fix (2026-04-24):** `_process_all_targets` now drains target tasks with
`asyncio.gather(..., return_exceptions=True)` instead of sequential awaits.
Unexpected per-target fetch/validation exceptions are converted into failed
`TargetResult`s with structured `ErrorRecord`s, so other completed target
results are preserved and existing `fail_strategy` handling determines the
final job status. Infrastructure failures that escape target processing are
still raised after all target tasks have been drained, and cancellation is not
converted into a normal failed target.

### 13. Circuit breaker state is in-memory only and not lock-protected
`src/scrapeyard/engine/resilience.py:118–154`

`CircuitBreaker` is a singleton (`api/dependencies.py:48–53`) shared across
concurrent `_process_target` coroutines. Its `_failures` and `_tripped_at`
dicts are mutated without any lock. The GIL makes single-op dict access
safe, but sequences like `check() → scrape → record_failure()` are not
atomic, so near-simultaneous failures on the same domain can bypass the
threshold or double-trip. State is also lost on every restart; a domain
that was cooling down will receive traffic immediately after a redeploy.

### 14. `RedisDomainRateLimiter` polls with a flat 0.1s sleep and no ceiling
`src/scrapeyard/engine/rate_limiter.py:48–74`

When many workers contend on the same domain, each loops
`GET → sleep(interval_remaining) → retry` or `SET NX → sleep(0.1) → retry`
with no jitter, no exponential backoff, and no loop cap. Under genuine
contention (think: 20 scheduled jobs all pointed at the same vendor at the
top of the hour after a scheduler jitter collision), this becomes a polling
hot loop that pressures Redis and burns CPU before anyone makes progress.

### 15. ~~Basic `Fetcher.get` has no explicit timeout~~ (RESOLVED)
`src/scrapeyard/engine/scraper.py:180–186`

```python
response = await loop.run_in_executor(
    None,
    lambda: fetcher_cls.get(url, **call_kwargs),
)
```

`call_kwargs` does not include `timeout`. If Scrapling's basic fetcher
defaults to `None` (httpx's default), a slow server hangs the thread
forever. Thread pool saturation is quiet — scrapes just stop completing.

Verify Scrapling's default; if it is not a sane finite value, set one
explicitly (e.g., 30s) for basic, and verify the browser timeout path
uses `BrowserConfig.timeout_ms` (it appears to, but should be tested).

**Fix (2026-04-24):** Scrapling's installed basic default was verified
finite (`Fetcher.get(..., timeout=10)`), and Scrapeyard now passes an
explicit `SCRAPEYARD_BASIC_FETCH_TIMEOUT_SECONDS` policy (default `30.0`)
into basic fetch calls. Browser fetch timeout behavior remains driven by
`BrowserConfig.timeout_ms` and is covered by `browser_fetch_kwargs` tests.

### 16. ~~Pagination loop can run forever on an empty `next` href~~ (RESOLVED)
`src/scrapeyard/engine/pagination.py`

The loop breaks on `not next_links` and `not next_url`, but does not break
when `next_url == current_url`. A site that renders a static "next" link
pointing at the current page produces a tight refetch loop up to
`max_pages`, which defaults to 10 but is user-set. Add an explicit check
that the resolved `next_url` is not equal to, nor a normalized variant of,
`current_url`.

**Fix (2026-04-24):** Pagination now tracks normalized seen URLs using a
key that lowercases scheme/host, strips fragments, drops default ports, and
preserves query strings. It stops before fetching self-links and also stops
before appending data when a fetched page redirects back to a seen URL.

### 17. `Path("/proc/self/statm").read_text()` in async hot path
`src/scrapeyard/queue/pool.py:63–74`

Called from `_check_memory()` through `enqueue()`. Sync file I/O blocks the event
loop. `/proc` is fast in practice but the pattern is wrong, and on
non-Linux (macOS dev, anyone running with psutil disabled) this silently
returns "memory fine" because of the `except ... return True`. A failed
read should at least log; silently defaulting to "accept everything"
defeats the whole memory-limit mechanism.

### 18. ~~Crash-recovery leaves jobs stuck `running` forever~~ (RESOLVED)
`src/scrapeyard/queue/worker.py:62–64, 497–501`, `src/scrapeyard/main.py:30–76`

Worker lease recovery relies on the 300s `workers_running_lease_seconds`
check inside `_should_skip_delivery`. But:

- A job that crashed after `UPDATE jobs SET status='running'` (worker.py:70)
  and before the `INSERT INTO job_runs` at line 77–88 leaves the `jobs` row
  in `running` with no matching `job_runs` row. Nothing ever rolls it back
  except a new delivery for that `run_id`, which may never happen for an
  ad-hoc job.
- The lifespan startup (`main.py:30–76`) does not scan for
  `job_runs.status='running'` older than 2× the grace period and mark them
  failed. After a crash, a scheduled job won't re-enqueue because the
  record says it's still running.

Needed: on startup, fail any `job_runs` with `status='running'` older than
a threshold, and reset the corresponding `jobs.status` if its
`current_run_id` matches.

**Fix (2026-04-24):** `SQLiteJobStore.recover_stale_running_jobs` now runs
at FastAPI lifespan startup after DB initialization and before worker/
scheduler startup. It marks stale `job_runs.status='running'` rows older
than 2× `SCRAPEYARD_WORKERS_RUNNING_LEASE_SECONDS` as failed, updates
matching `jobs.status='running'` rows when `current_run_id` matches, and
also fails stale `running` jobs whose `current_run_id` has no active run
row because the process crashed before `job_runs` insertion.

### 19. `extra_hosts: host.docker.internal` and a hard-coded `redis://redis:...` default
`docker-compose.yml:15–16, 21`, `src/scrapeyard/common/settings.py:26`

Default `redis_dsn` is `redis://redis:6379/0` (a docker-compose hostname).
Outside the bundled compose file this is nonsense — any other deployment
(K8s, ECS, a host-networking dev laptop) must override it. `extra_hosts:
host.docker.internal:host-gateway` is a Desktop/WSL shim that doesn't
belong in a production compose file. Needed: an empty-string default and
explicit failure if unset, plus removal of the `extra_hosts` entry from
the production file.

### 20. `importlib.resources` traversal to `../../sql` is fragile
`src/scrapeyard/storage/database.py:66–68`, `Dockerfile:43`

```python
sql_dir = importlib.resources.files("scrapeyard") / "../../sql"
sql_dir = Path(str(sql_dir)).resolve()
```

Using `..` traversal from a package resource path depends on how the
package is installed (editable vs wheel), and the Dockerfile compensates
with a `cp -r sql/ ...` to the exact path that resolves from inside
site-packages. This works today; it will break silently the next time
anyone reorganizes the repo or switches to a zipapp/wheel-only install.
Package the SQL files as real package data (move to `src/scrapeyard/sql/`
and use `importlib.resources.files(...).joinpath(...)`).

---

## MEDIUM — Fix Soon

### 21. No observability — no metrics, no tracing
Nothing exports Prometheus metrics or OpenTelemetry traces. In production,
the only signal is log lines. We'll have no queue-depth gauge, no
per-target latency histogram, no success/failure rate, no way to alert on
"scrape success rate dropped below X". Add at minimum: queue depth, job
success/failure counters by project, scrape latency histogram, active
browsers gauge, and expose `/metrics`. Scrapeyard will be the hardest
service to debug without this.

### 22. No CORS policy, no security headers
`src/scrapeyard/main.py:78–85`. No `CORSMiddleware`, no
`X-Content-Type-Options`, no `Strict-Transport-Security`, etc. Decide:
this service is API-only, EyeBox is the only caller, so either add a
strict CORS policy scoped to the EyeBox origin or explicitly reject
cross-origin requests (the latter is probably correct).

### 23. `job_runs` has no foreign key to `jobs`
`sql/004_create_job_runs.sql`. `DELETE FROM jobs WHERE job_id=?`
(`storage/job_store.py`) leaves orphan rows in `job_runs`, `errors`, and
(indirectly via `job_id`) result files. Add a `FOREIGN KEY(job_id)
REFERENCES jobs(job_id) ON DELETE CASCADE` and enable `PRAGMA
foreign_keys = ON` at connection time (see #3). Note: enabling FK on an
existing DB requires a migration step since pre-existing rows may be
orphaned.

### 24. No migration versioning
`src/scrapeyard/storage/database.py:14–18, 70–75`. Migrations are a
hard-coded list of SQL files, each guarded by `CREATE TABLE IF NOT
EXISTS`. Any modification to an already-applied migration is silently
skipped. Once there's production data we cannot alter or drop columns
without a rewrite. Adopt a minimal `schema_migrations(name, applied_at)`
table and append-only migrations, or pull in a library (alembic,
yoyo-migrations). Doing this before the first production write is cheap;
doing it after is painful.

### 25. Cleanup loop holds the per-db async lock while touching the filesystem
`src/scrapeyard/storage/cleanup.py:47–71`. The `async with get_db` on line
90 spans the whole `run_cleanup`. The filesystem work is offloaded with
`asyncio.to_thread`, which is good, but the DB lock is still held. A
long-running cleanup blocks every other writer to `results_meta.db`.
Split: read the candidate rows in one transaction, release the lock, do
the filesystem work, then reacquire for the DELETE.

### 26. Cron expressions not validated on create
`src/scrapeyard/api/routes.py:218`. `SchedulerService.register_job` passes
the raw string into APScheduler. An invalid cron string may raise
asynchronously inside the scheduler and make the job unschedulable with no
clear feedback to the caller. Validate with `croniter.is_valid` or
APScheduler's parser before persisting.

### 27. Exception text leaked into 4xx responses
`src/scrapeyard/api/routes.py:107–112, 184–189`. The handler returns
`"Invalid config: {exc}"` where `exc` is the raw Pydantic/yaml error. This
leaks internal schema structure. Log the exception server-side, return a
minimal message. (Less of a risk once auth is added, but still noisy.)

### 28. No disk-space backpressure
Nothing checks free space on `/data` before accepting a job or writing a
result. `result_store.save_result` will raise `OSError: ENOSPC`, the outer
try/except marks the job failed, and the loop keeps accepting work. Add a
`shutil.disk_usage` check to `WorkerPool._check_memory()` and to `/health`.

### 29. Default `SCRAPEYARD_WORKERS_MEMORY_LIMIT_MB=4096` without a container memory limit
`docker-compose.yml` (no `deploy.resources.limits`). The Python-level soft
limit rejects new jobs over 4GB RSS, but nothing stops Chromium or a
runaway scrape from ballooning past that. The host kernel OOM killer ends
the process ungracefully, dropping in-flight work with no lease cleanup.
Set a container memory limit ~1.2–1.5x the Python soft limit.

### 30. Deep dependency version pins are loose
`pyproject.toml`. Caret constraints on everything. This is fine for a
pre-production codebase but will bite us when `poetry lock` runs on a
fresh checkout post-launch and pulls new FastAPI/arq/scrapling/playwright
minor versions. Pin the direct dependencies or commit the lockfile-driven
install path and rebuild the image from lockfile, not `poetry export`.

### 31. Python 3.10 still allowed but EOL is imminent
`pyproject.toml:10` (`python = ">=3.10,<4.0"`). 3.10 reaches EOL in 2026.
Container uses 3.12 but tests/CI allow 3.10. Narrow the range to
`>=3.12,<4.0` to avoid someone using a supported-but-soon-dead interpreter.

### 32. Log level and log dir not environment-configurable
`src/scrapeyard/common/logging.py:22` hard-codes `logging.INFO`. Add a
`SCRAPEYARD_LOG_LEVEL` setting. Without it we can neither crank verbosity
for an incident nor quiet down noisy paths in production.

### 33. ~~No startup Redis-connect timeout~~ (RESOLVED)
`src/scrapeyard/queue/pool.py:85–88`. `await create_pool(...)` has no
timeout. If Redis is unreachable at boot, the lifespan hangs and K8s will
repeatedly start, fail its liveness probe (once added — see #6), and get
reaped, producing a crash loop instead of a clear error. Wrap with
`asyncio.wait_for` and log the failure.

**Fix (2026-04-24):** `WorkerPool.start()` wraps Redis `create_pool` in
`asyncio.wait_for` using `SCRAPEYARD_WORKERS_REDIS_CONNECT_TIMEOUT_SECONDS`
(default `10.0`) and logs timeout/connection failures clearly before worker
construction or `_started` mutation.

### 34. `_start_time` and `_projects_cache` are module globals mutated from a lifespan
`src/scrapeyard/main.py:24–26, 33–34, 87–136`. Low priority, but these
make the app non-reloadable inside a test run without explicit reset, and
they interact with `lru_cache`d dependencies in `api/dependencies.py`.
Not blocking; note for the same pass that fixes test teardown.

### 35. No CI pipeline
No `.github/workflows/`, no GitLab CI. Tests only run locally. Add at
minimum: `ruff`, `pytest` (unit + integration), and a Docker build on PR.
Without this, dependency pin bumps and behavior regressions ship silently.

---

## Summary

| # | Area | Severity | File:line | Status |
|---|---|---|---|---|
| 1 | No API auth | BLOCKER | api/middleware.py | ✅ resolved |
| 2 | SSRF on target/webhook URLs | BLOCKER | engine/url_guard.py | ✅ lexical+DNS |
| 3 | SQLite missing WAL/busy_timeout | BLOCKER | storage/database.py | ✅ resolved |
| 4 | No request body size limit | BLOCKER | api/middleware.py | ✅ resolved |
| 5 | Broken JSON log format | BLOCKER | common/logging.py | ✅ already fixed |
| 6 | `/health` doesn't probe deps | BLOCKER | runtime/health.py, main.py | ✅ resolved |
| 7 | Non-atomic result file writes | BLOCKER | storage/filesystem.py | ✅ resolved |
| 8 | Container runs as root | BLOCKER | Dockerfile | ✅ already fixed |
| 9 | Proxy creds served plain | BLOCKER | engine/url_guard.py, api/serializers.py | ✅ resolved |
| 10 | Webhook fire-and-forget | HIGH | webhook/dispatcher.py, storage/webhook_outbox.py | ✅ resolved |
| 11 | No HTTP rate limiting | HIGH | api/middleware.py, main.py | ✅ resolved |
| 12 | Sequential `await task` loop | HIGH | worker.py | ✅ resolved |
| 13 | Circuit breaker in-memory + unlocked | HIGH | resilience.py:118–154 |
| 14 | Rate limiter polling loop | HIGH | rate_limiter.py:48–74 |
| 15 | No basic-fetcher timeout | HIGH | scraper.py:180–186 | ✅ resolved |
| 16 | Pagination can refetch same URL | HIGH | pagination.py | ✅ resolved |
| 17 | Sync `/proc` read in async hot path | HIGH | pool.py:63–74 |
| 18 | Crashed runs stuck `running` | HIGH | worker.py + main.py lifespan | ✅ resolved |
| 19 | Prod defaults pointed at compose-only values | HIGH | settings.py:26, compose:15 |
| 20 | `importlib.resources` traversal to `sql/` | HIGH | database.py:66, Dockerfile:43 |
| 21 | No metrics / tracing | MEDIUM | n/a |
| 22 | No CORS / security headers | MEDIUM | main.py:78–85 |
| 23 | No FK on `job_runs` | MEDIUM | sql/004 |
| 24 | No migration versioning | MEDIUM | database.py:14–18 |
| 25 | Cleanup holds DB lock during FS work | MEDIUM | cleanup.py:47–71 |
| 26 | Cron not validated at create | MEDIUM | routes.py:218 |
| 27 | Exception text leaked to clients | MEDIUM | routes.py:107, 184 |
| 28 | No disk-space backpressure | MEDIUM | pool.py, result_store.py |
| 29 | No container memory limit | MEDIUM | docker-compose.yml |
| 30 | Loose dep caret pins | MEDIUM | pyproject.toml |
| 31 | Python 3.10 allowed | MEDIUM | pyproject.toml:10 |
| 32 | Log level not env-configurable | MEDIUM | logging.py:22 |
| 33 | No Redis-connect startup timeout | MEDIUM | pool.py:85–88 | ✅ resolved |
| 34 | Module-global lifespan state | MEDIUM | main.py:24–26 |
| 35 | No CI | MEDIUM | n/a |

Recommended launch gate: resolve all BLOCKERs (1–9), plus #10 (webhook
delivery guarantee — EyeBox depends on knowing scrapes finished), #11
(rate limiting), and #18 (crash recovery). Everything else can be the
first post-launch iteration.

**Launch gate progress (2026-04-24):**
- BLOCKERs 1–9: all resolved.
- HIGH #10: resolved — `HttpWebhookDispatcher.submit(...)` durably writes a
  `webhook_deliveries` outbox row before scheduling delivery; startup replays
  pending rows, success marks delivered, retryable failures remain pending with
  backoff, and permanent 4xx failures remain inspectable.
- HIGH #11: resolved — in-memory inbound HTTP rate limiting now runs before
  auth/router work, keyed by known API key or client IP.
- HIGH #18: resolved — startup recovery now fails stale running runs/jobs
  before worker/scheduler startup.
