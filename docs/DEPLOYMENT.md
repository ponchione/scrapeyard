# Deployment Hardening

Scrapeyard is intended to run as an internal worker service consumed by
Eyebox, not as a public internet API. The deployment should enforce that
assumption at the network layer.

## Required Topology

- Eyebox is the only service that can reach Scrapeyard HTTP traffic.
- Redis is private to Scrapeyard and is not exposed outside the internal
  runtime network.
- Scrapeyard stores `/data` on persistent storage.
- Scrapeyard runs exactly one application process and one replica. Keep
  Uvicorn/Gunicorn workers and orchestrator replicas at `1`; use
  `SCRAPEYARD_WORKERS_MAX_CONCURRENT` for in-process scrape concurrency.
- Browser scraping egress is controlled by the host, orchestrator, proxy
  gateway, or firewall policy and attested before the application starts.

## Single-instance guard

Before opening SQLite or starting the embedded worker pool and scheduler, the
process acquires `SCRAPEYARD_DB_DIR/.scrapeyard-instance.lock`. A second process
sharing `/data/db` exits immediately with an actionable owner PID/host message.
The persistent file is not a PID-file validity check: ownership is a kernel
advisory lock, so normal shutdown and crashes both release it and stale file
contents are safely overwritten on restart. The backing volume must preserve
Linux `flock(2)` semantics.

The image entrypoint and application lifespan also reject common
`WEB_CONCURRENCY`, Uvicorn, and Gunicorn configurations above one process.
`docker-compose.yml` fixes both `--workers 1` and `deploy.replicas: 1`. Do not
override those settings or scale the service. To run a truly isolated second
deployment, give it separate `/data` storage, Redis logical database, and queue
name; partial sharing is unsupported.

The ownership table and the architectural work required for future API,
scheduler, and worker separation are documented in [SCALING.md](SCALING.md).

## Ingress

- Do not make `8420` reachable from untrusted networks.
- The production `docker-compose.yml` exposes no host port. The explicit
  `docker-compose.local.yml` override binds `127.0.0.1:8420` by default:

  ```bash
  docker compose -f docker-compose.yml -f docker-compose.local.yml up -d
  ```

  A wider `SCRAPEYARD_BIND_ADDRESS=0.0.0.0` is an explicit local-development
  choice and requires host firewall restrictions. This overlay explicitly
  selects trusted-input mode and disables the production egress probe; do not
  use it as a production override.

- For a shared deployment with Eyebox, prefer a private service network over a
  host port. If Eyebox shares Scrapeyard's Compose network, use
  `http://scrapeyard:8420` instead of `host.docker.internal`. If a reverse proxy
  is used, restrict the proxy route to Eyebox. Public `/health` and
  `/health/live` reveal only process liveness; protect `/health/ready` for
  internal monitoring.
- Always set `SCRAPEYARD_API_CREDENTIALS`; when no named or legacy credentials
  exist, the warned `local-development` caller receives all scopes.

## Egress

App-level URL guards are a backstop, not the only control. Production Compose
defaults `SCRAPEYARD_UNTRUSTED_SUBMISSIONS=true`, requires an egress-filtering
operator `SCRAPEYARD_PROXY_URL`, and points a startup attestation at the
controlled private `egress-probe` service. That helper owns a denied challenge
listener and a distinct, narrowly allowed liveness protocol. The application
refuses to start if the proxy is empty/`direct`, if any probe setting is
incomplete, if liveness or protocol validation fails, or if the challenge
accepts a connection.

In this mode, caller-submitted job/target proxies and `browser.cdp_url` require
the separate `transport-admin` scope. Ordinary submitters cannot select a
transport endpoint and all targets inherit the operator proxy. The proxy must
itself resolve target names and reject private, loopback, link-local, and cloud
metadata destinations; otherwise it becomes an SSRF relay.

Config parsing performs only bounded lexical, scheme, hostname, and literal-IP
checks; it never performs DNS resolution in Pydantic validators. Hostnames are
resolved and checked freshly immediately before transport use. Direct basic
HTTP requests connect to the validated address with the original Host header
and TLS SNI. Browser navigation, redirects, subresources, service workers,
CDP, and proxy connections may resolve independently, so only a connected-IP
host/orchestrator boundary closes their validation-to-connection races.

Enforce outbound network policy for the Scrapeyard container or pod:

- Allow Redis.
- Allow only the controlled probe's liveness port; keep its challenge port
  subject to the private-address denial.
- Allow the configured proxy gateway if scraping through a proxy.
- Allow public HTTP/HTTPS destinations needed for scraping.
- Block cloud metadata and link-local ranges, including `169.254.169.254` and
  `169.254.0.0/16`.
- Block private/internal ranges unless explicitly required:
  `127.0.0.0/8`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, IPv6
  loopback, link-local, and ULA ranges.
- Block special-purpose documentation, transition, multicast, benchmark, and
  reserved destinations, including `192.0.2.0/24`, `198.51.100.0/24`,
  `203.0.113.0/24`, IPv4-mapped IPv6, and NAT64 forms of denied IPv4 ranges.

Route all untrusted scrape traffic through the configured proxy gateway. Where
the host policy can use a public allowlist, allow Scrapeyard egress only to that
gateway plus Redis. This keeps browser runtime and target DNS behavior outside
the application trust boundary.

The checked-in `security/deploy-secure-compose.sh` makes installation and
attestation one deployment transaction. On redeploy it builds the replacement
image, stops any existing application container, starts only Redis and the
controlled private probe, discovers the deployment bridge, installs the
checked-in bridge-and-source-scoped `DOCKER-USER` policy, and only then starts
the app.
The app independently verifies the helper and its challenge listener over the
liveness port, attempts the challenge from its own network namespace, then
verifies liveness again. A reachable challenge, a merely closed/unroutable
helper, or helper death during attestation aborts lifespan startup. Export the
operator proxy (and public proxy CIDR when needed), then run the wrapper as
root:

```bash
export SCRAPEYARD_PROXY_URL=https://scrape-proxy.example:8443
export SCRAPEYARD_EGRESS_ALLOW_CIDRS=203.0.113.10/32
sudo --preserve-env=SCRAPEYARD_PROXY_URL,SCRAPEYARD_EGRESS_ALLOW_CIDRS \
  security/deploy-secure-compose.sh
```

The installed policy permits the fixed Redis peer, the controlled helper's
liveness port, and operator-declared destinations before rejecting loopback,
metadata/link-local, private, carrier-grade NAT, benchmark, multicast, and
reserved connected addresses. The operator allow example above intentionally
uses an address inside a denied documentation range, so it also demonstrates
that explicit allows precede the reviewed deny policy.

The reviewed address table in `src/scrapeyard/engine/ip_policy.py` is shared by
the application predicate and the host-policy renderer. It is fixed to the IANA
special-purpose registries reviewed on 2025-10-09 instead of changing with the
host Python version's `ipaddress.is_global` tables. It is intentionally stricter
for a few special-purpose blocks whose isolated public exceptions are not
useful scrape targets, including all of `192.0.0.0/24`.

Policy replacement uses alternating staging chains. The installer fully builds
the inactive chain, inserts and confirms its `DOCKER-USER` jump while the old
jump is still present, and removes the old jump only afterward. An error trap
reinserts the prior jump before withdrawing the replacement if any later IPv4
or IPv6 mutation fails. Address families, tools, Docker chain prerequisites,
and rendered rules are validated before the active policy is changed.

The helper reports liveness only while its
challenge listener is serving, so connection refusal or timeout is accepted as
policy proof only between two valid liveness exchanges. A hostname that returns
public during validation and private during connection is therefore rejected.
A direct `docker compose up` with production defaults is not a supported
deployment transaction: without the rule the reachable challenge keeps the app
unhealthy, and without the proxy settings validation fails.

During decommissioning, stop the stack before removing the rule. Resolve the
bridge exactly as the wrapper does (or retain its exported values), then run:

```bash
NETWORK_ID=$(docker network inspect --format '{{.Id}}' scrapeyard_backend)
sudo SCRAPEYARD_EGRESS_INTERFACE="br-${NETWORK_ID:0:12}" \
  SCRAPEYARD_EGRESS_POLICY_ID=prod \
  security/install-docker-egress-policy.sh remove
```

Treat this policy or an equivalent orchestrator policy as mandatory for
untrusted submissions. The authorization boundary applies when YAML is
accepted; it does not retroactively rewrite persisted schedules. Audit existing
scheduled configurations before enabling the mode, and recreate intentional
proxy/CDP overrides with a `transport-admin` credential.

Kubernetes deployments should express the same allow-before-deny policy with a
CNI that supports egress CIDR rules; default Kubernetes `NetworkPolicy` cannot
select arbitrary public destinations while excluding every rebinding target.

## Retained release artifacts and rollback

Prepare a release on a build/qualification host with Docker, Python 3.10+, and
at least 15 GiB free disk. Use an isolated host or ensure the qualification
lane's fixed subnets and ports are unused. This builds the selected **committed**
revision, runs the existing security scans and complete quick qualification,
and publishes the directory only after both pass:

```bash
python3 scripts/release_artifact.py create /srv/scrapeyard-releases/candidate --revision HEAD
python3 scripts/release_artifact.py verify /srv/scrapeyard-releases/candidate
```

The directory contains the source revision's files (including Compose, SQL,
entrypoint and security policy), bounded scan/qualification reports, the
application/Redis/probe image archive, and a SHA-256/permission manifest. The
Compose overlay selects all three images by their immutable local image IDs.
Image archives use Docker's native [save](https://docs.docker.com/reference/cli/docker/image/save/)
and [load](https://docs.docker.com/reference/cli/docker/image/load/) commands.
Checksums detect corruption; obtain the manifest and bundle through a trusted
channel. They are not a publisher signature. Preserve file modes when copying.
Never add `.env`, credentials, databases, or backup sets to the bundle.

Retain the current and previous known-good directories on the deployment host
and in protected off-host storage. Keep enough disk for both archives, loaded
images, runtime growth and the coordinated pre-release backup. An incomplete
or failed preparation never replaces a retained release. Local archives do not
expire automatically; prune only releases outside the agreed rollback window.

Before promotion, pause new ingress/dispatch, complete the
[quiesced backup procedure](TESTING.md#quiesced-backup-and-fresh-restore), and
verify access to the separate [encryption key recovery material](SECRET_STORAGE.md#rotation-backup-restore-and-key-loss).
Record the current/candidate manifest identifiers and the backup location.
Export the existing deployment settings and credentials through the normal
protected environment. For a protected external Compose env file, use an
absolute `COMPOSE_ENV_FILES` path; the wrapper still requires the operator
`SCRAPEYARD_PROXY_URL` in its environment. Use the **same existing Compose
project name** so application and Redis volumes retain their owners:

```bash
export COMPOSE_PROJECT_NAME=scrapeyard
# Run as root with the deployment environment already supplied securely.
security/deploy-secure-compose.sh --release /srv/scrapeyard-releases/candidate
```

The wrapper verifies and loads the bundle, checks all images before stopping
the app, starts dependencies, installs the retained egress/AppArmor policies,
then starts the app with `--no-build --pull never`. The source and `images.json`
paths in the bundle are the only Compose files selected in release mode. A
failure before stopping leaves the current app running; a policy failure
leaves it stopped. Do not resume ingress until authenticated `/health/ready`,
a known `/results/{id}` read, effective host/security policy, and exactly one
application container/process have been verified. Compose health alone tests
liveness. Retain redacted evidence of those checks after each transition.

Rollback uses the identical command with the **previous known-good bundle**;
no build, registry, dependency or browser downloads are needed. Keep its source
directory in place while deployed because Compose mounts its probe/security
files. If candidate readiness or result verification fails, keep ingress
paused and follow the compatible-binary or data-restore decision below.

SQLite migrations are forward-only and validated against a checksummed ledger.
An older binary may reject a newer schema. Before selecting binary rollback,
compare both bundles' SQL files and qualify the older binary against an
isolated copy of the candidate's data. Identical SQL alone is not proof of
application data compatibility. This release-tooling change adds no migration.
When compatibility is unproven, keep the app stopped and either forward-repair
or restore the coordinated pre-release application **and corresponding Redis**
state with its keys into fresh volumes. Validate the backup using its matching
release's helper/SQL, rehearse known jobs/results/decryption and queue recovery
in isolation, then switch the deployment to the recovered volumes. Never try
to downgrade by editing the migration ledger or restore over live writers.

The artifact's fixture qualification does not qualify production proxy,
capacity, ingress, monitoring or off-host backup installation. TD-22 remains
open until candidate deployment, failed-candidate rejection and compatible
rollback pass under the intended deployment controls without build-network
access. Keep this operator evidence separately from automated fixture reports.

## Container privilege and filesystem boundary

The image is built for Linux `amd64` and runs directly as UID/GID `10001`.
Chromium uses unprivileged user namespaces, so the image contains no privileged
setuid helper and startup performs no root repair. Before starting Compose,
load the path-independent host profile:

```bash
sudo security/install-chromium-apparmor-profile.sh install
```

The profile is Moby's normal container boundary plus the single `userns`
permission. Compose also applies Playwright 1.58.0's pinned seccomp allowlist,
enables no-new-privileges, drops every capability, and retains only
`SYS_CHROOT` in the bounding set for Chromium after it enters its user
namespace. The application process has no permitted or effective capabilities.
Do not replace either policy with `unconfined`. `/data`, bounded `/tmp`, `/run`,
home cache/config and Camoufox state tmpfs, and `/dev/shm` are the only writable
areas.

Fresh named volumes inherit UID 10001 from the image. Migrate an older or host
bind-mounted data directory while the application is stopped:

```bash
docker run --rm --user 0 -v scrapeyard_scrapeyard-data:/data \
  redis:7.4.5-alpine@sha256:bb186d083732f669da90be8b0f975a37812b15e913465bb14d845db72a4e3e08 \
  sh -c 'chown -R 10001:10001 /data && find /data -type d -exec chmod 0750 {} +'
```

Startup fails with exit code 78 and an actionable message if ownership is
wrong; it never recursively changes a mounted volume. CPU, memory, PIDs,
open-files, shared memory, and temporary-filesystem sizes are bounded in
Compose. Update the pinned digests and browser checksums intentionally and run
the container smoke plus security scan before promotion.

Remove the host profile only after all Scrapeyard containers have stopped:

```bash
sudo security/install-chromium-apparmor-profile.sh remove
```

## Secrets

- Generate API keys with a high-entropy value:

  ```bash
  openssl rand -hex 32
  ```

- Store `SCRAPEYARD_API_CREDENTIALS` in the deployment secret store, not in
  source control. It is a JSON object keyed by credential name:

  ```json
  {
    "eyebox-old": {
      "identity": "eyebox",
      "secret": "high-entropy-old-secret",
      "scopes": ["submit", "read"],
      "projects": ["catalog"]
    },
    "eyebox-new": {
      "identity": "eyebox",
      "secret": "high-entropy-new-secret",
      "scopes": ["submit", "read"],
      "projects": ["catalog"]
    }
  }
  ```

- `identity` is stable audit/idempotency attribution. During rotation, deploy
  old and new named credentials with the same identity, move clients to the
  new secret, then remove and restart without the old entry. Authentication
  compares every configured secret in constant time before selecting a match.
- Available scopes are `submit`, `read`, `schedule-admin`, `delete`,
  `health-detail`, and `transport-admin`. Reserve `transport-admin` for
  operators who may select a per-job/per-target proxy or CDP endpoint while
  untrusted-submission mode is enabled. An optional non-empty `projects` list limits route and data
  access to those namespaces. Project-scoped list/error queries must include a
  permitted `project` filter.
- `SCRAPEYARD_API_KEYS` remains a deprecated, full-admin restart-time migration
  bridge. It produces digest-based legacy identities and should not be used for
  new deployments.
- Failed authentication is counted per process and logged with reason, method,
  and path. Successful request audit logs include stable identity and
  credential name. Neither path logs the supplied secret.
- Treat proxy URLs, webhook headers, and browser extra headers as secrets.
- Set `SCRAPEYARD_SECRET_REFERENCE_ALLOWLIST` to a JSON project-to-name map
  before accepting YAML that uses `${SCRAPEYARD_SECRET_*}`. References are
  denied by default; use the reserved `*` project only for deliberately shared
  names. The setting contains names, never secret values.

Persisted config and webhook retry state require the versioned encryption
keyring described in [SECRET_STORAGE.md](SECRET_STORAGE.md). Store key material
separately from database backups, test restore/decryption, and follow the
overlap/re-encryption procedure before retiring a key. Prefer allowlisted
`${SCRAPEYARD_SECRET_*}` references in YAML so reusable values are resolved at
execution instead of embedded in stored config.
Startup validates both encryption settings and the active 32-byte key before
opening the databases or starting Redis/workers/scheduler, including on a
pristine deployment. Missing, partial, malformed, or inactive keyrings prevent
the process from becoming ready.

Use the cheap public `/health/live` endpoint only for process liveness. Route
traffic only after the authenticated `/health/ready` endpoint succeeds; it
checks Redis, every SQLite database, artifact storage read/write, disk space,
and every required background task with short timeouts. Scrape the authenticated
Prometheus `/metrics` endpoint and apply the saturation/backlog/disk alerts in
[MONITORING.md](MONITORING.md).

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
- `SCRAPEYARD_WORKERS_MAX_ACCEPTED_RUNS` (default `100`, queued plus running owners)
- `SCRAPEYARD_WORKERS_MEMORY_LIMIT_MB`
- `SCRAPEYARD_WORKERS_BROWSER_MEMORY_RESERVE_MB` (default `512`, per active browser target)
- `SCRAPEYARD_WORKERS_SHUTDOWN_GRACE_SECONDS` (default `30`, shared by worker and webhook shutdown)
- `SCRAPEYARD_WORKERS_CANCELLATION_GRACE_SECONDS` (default `10`)
- `SCRAPEYARD_WORKERS_QUEUED_CLAIM_TIMEOUT_SECONDS` (default `300`)
- `SCRAPEYARD_WORKERS_QUEUE_PAYLOAD_TTL_SECONDS` (default `604800`)
- `SCRAPEYARD_WORKERS_QUEUED_RECONCILIATION_INTERVAL_SECONDS` (default `60`)
- `SCRAPEYARD_WORKERS_QUEUED_RECONCILIATION_BATCH_SIZE` (default `100`)
- `SCRAPEYARD_WORKERS_RUNNING_HEARTBEAT_TIMEOUT_SECONDS` (default `600`)
- `SCRAPEYARD_WORKERS_RUNNING_RECONCILIATION_INTERVAL_SECONDS` (default `60`)
- `SCRAPEYARD_WORKERS_RUNNING_RECONCILIATION_BATCH_SIZE` (default `100`)
- `SCRAPEYARD_WORKERS_HEARTBEAT_INTERVAL_SECONDS` (default `30`)
- `SCRAPEYARD_MAX_REQUEST_BYTES`
- `SCRAPEYARD_RUN_MAX_DURATION_SECONDS` (default `900`)
- `SCRAPEYARD_RUN_MAX_FETCHED_BYTES` (default `104857600`, 100 MiB)
- `SCRAPEYARD_RUN_MAX_EXTRACTED_RECORDS` (default `100000`)
- `SCRAPEYARD_RUN_MAX_SERIALIZED_RESULT_BYTES` (default `52428800`, 50 MiB)
- `SCRAPEYARD_RUN_MAX_BROWSER_DEBUG_BYTES` (default `26214400`, 25 MiB)
- `SCRAPEYARD_API_RESULT_THREAD_MAX_WORKERS` (default `2`)
- `SCRAPEYARD_TRANSFORM_REGEX_TIMEOUT_SECONDS` (default `0.1` per operation)
- `SCRAPEYARD_TRANSFORM_REGEX_MAX_PATTERN_BYTES` (default `2048` UTF-8 bytes)
- `SCRAPEYARD_TRANSFORM_MAX_PIPELINE_STEPS` (default `32`)
- `SCRAPEYARD_TRANSFORM_MAX_VALUE_BYTES` (default `1048576`, 1 MiB per intermediate value)
- `SCRAPEYARD_WEBHOOK_MAX_DELIVERY_ATTEMPTS` (default `5`)
- `SCRAPEYARD_WEBHOOK_MAX_DELIVERY_AGE_SECONDS` (default `86400`)
- `SCRAPEYARD_WEBHOOK_DISPATCH_CONCURRENCY` (default `4`)
- `SCRAPEYARD_WEBHOOK_DISPATCH_BATCH_SIZE` (default `100`, and no smaller than concurrency)
- `SCRAPEYARD_WEBHOOK_CLIENT_CACHE_MAX_SIZE` (default `64`)
- `SCRAPEYARD_WEBHOOK_CLIENT_CACHE_IDLE_TTL_SECONDS` (default `300`)
- `SCRAPEYARD_WEBHOOK_DELIVERED_RETENTION_DAYS` (default `7`)
- `SCRAPEYARD_WEBHOOK_FAILED_RETENTION_DAYS` (default `30`)
- `SCRAPEYARD_IDEMPOTENCY_KEY_MAX_BYTES` (default `128`)
- `SCRAPEYARD_IDEMPOTENCY_RETENTION_HOURS` (default `24`)
- `SCRAPEYARD_IDEMPOTENCY_CLEANUP_BATCH_SIZE` (default `1000`)
- `SCRAPEYARD_HISTORY_ADHOC_JOB_RETENTION_DAYS` (default `30`)
- `SCRAPEYARD_HISTORY_SCHEDULED_RUN_RETENTION_DAYS` (default `30`)
- `SCRAPEYARD_HISTORY_SCHEDULED_RUN_RETENTION_COUNT` (default `100`)
- `SCRAPEYARD_HISTORY_ERROR_RETENTION_DAYS` (default `30`)
- `SCRAPEYARD_HISTORY_WEBHOOK_TOMBSTONE_RETENTION_DAYS` (default `30`)
- `SCRAPEYARD_HISTORY_ADHOC_JOB_CLEANUP_BATCH_SIZE` (default `100`)
- `SCRAPEYARD_HISTORY_SCHEDULED_RUN_CLEANUP_BATCH_SIZE` (default `500`)
- `SCRAPEYARD_HISTORY_ERROR_CLEANUP_BATCH_SIZE` (default `1000`)
- `SCRAPEYARD_SCHEDULER_MISFIRE_GRACE_SECONDS` (default `60`)
- `SCRAPEYARD_STORAGE_RETENTION_DAYS` (default `30`, must be positive)
- `SCRAPEYARD_STORAGE_MAX_RESULTS_PER_JOB` (default `100`, must be positive)
- `SCRAPEYARD_STORAGE_CLEANUP_BATCH_SIZE` (default `500`)
- `SCRAPEYARD_STORAGE_ORPHAN_GRACE_SECONDS` (default `86400`)
- `SCRAPEYARD_STORAGE_RECONCILIATION_DRY_RUN` (default `true`)
- `SCRAPEYARD_STORAGE_RECONCILIATION_MAX_ENTRIES_PER_RUN` (default `10000`)
- `SCRAPEYARD_HEALTH_DISK_FREE_MIN_MB`

Queued delivery replacement and running execution recovery use independent
clocks. `SCRAPEYARD_WORKERS_QUEUED_CLAIM_TIMEOUT_SECONDS` applies only while a
delivery has not been claimed. Once a worker atomically claims a run, its
persisted `heartbeat_at` is authoritative and the scheduler may recover it only
after `SCRAPEYARD_WORKERS_RUNNING_HEARTBEAT_TIMEOUT_SECONDS` without a
successful refresh. The heartbeat interval must be no more than one third of
the running timeout.

### Scheduled-job lifecycle and timezones

Every schedule has an explicit IANA `timezone`; omitted values default to
`UTC`. Cron fields are interpreted as wall time in that zone and the timezone
is persisted and returned by job APIs. With APScheduler 3 semantics, a spring
forward wall time that does not exist resolves to the corresponding next real
instant (for example New York `02:30` fires at `03:30` on the transition day),
while a repeated fall-back wall time fires once in each fold. Jitter is applied
after cron selection.

The in-process scheduler uses `coalesce=true`, `max_instances=1`, and the
bounded `SCRAPEYARD_SCHEDULER_MISFIRE_GRACE_SECONDS`. Multiple due occurrences
caused by a short event-loop stall coalesce to one fire if it is still within
that grace. Because the scheduler store is deliberately reconstructed from
SQLite rather than persisted separately, process downtime is not backfilled:
restart registers each job at its next future wall-time occurrence. Clock/DST
selection follows the zone rules above.

Every cron fire runs inside a failure boundary and receives a run ID before
persisted configuration is loaded. A failure before Redis acceptance creates a
terminal scheduled attempt with a bounded `failure_code`, and records
`schedule_failure_at`, `schedule_failure_code`, and
`schedule_consecutive_failures` on the parent. Those fields are returned by the
job APIs, survive restart, and make scheduler readiness fail until that same
enabled schedule accepts a later cron run. Pausing a broken schedule removes it
from readiness while preserving its durable failure fields; resuming exposes
the failure again until a successful fire. Exception messages and resolved
secret values are never persisted. Manual triggers retain their synchronous
error contract instead of being contained as cron callbacks.

`PUT /jobs/{job_id}` replaces the complete YAML and schedule for future runs;
it returns the SHA-256 `config_hash` that subsequent runs will record. An
already queued or running delivery makes update return `409`, so accepted work
never changes configuration underneath itself. The `(project, name)` unique
constraint remains authoritative. A job's `project` namespace is immutable;
an update that supplies a different project returns `409`, preserving the
ownership of its historical runs, results, and errors. `POST .../pause` and
`.../resume` persist state and re-register the local scheduler, and therefore
survive restart;
pause affects future fires, not a run already accepted. `POST .../trigger`
works even while cron is paused, returns `202` with `trigger=manual`, `run_id`,
and the exact config hash, and returns `409` when a run is already queued or
active.

Database mutation precedes local scheduler registration in one event-loop
turn. If registration fails, the API compare-and-set restores the prior
database snapshot and re-registers it; a failed create removes its never-run
row. Failure to regain exact ownership is logged at critical severity and
returns `503` with an explicit operator-reconciliation action instead of
silently accepting divergent state.

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

New run admission reserves one current queued/running owner under the same
SQLite write transaction that saves the job/run snapshot. The partial admission
index excludes terminal history. `SCRAPEYARD_WORKERS_MAX_ACCEPTED_RUNS` defaults
to 100; size it using the qualified workload and observed queue age. Lowering it
below existing occupancy blocks new submissions until existing work drains.
Idempotent replay, missing-delivery recovery, and cancellation keep working when
full; [the API overload contract](API.md#admission-overload) describes retries.

Memory admission and worker-start checks use the process's cgroup memory usage,
including browser subprocesses, file cache, and kernel charges. Both
[cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html) and
[cgroup v1](https://docs.kernel.org/admin-guide/cgroup-v1/memory.html) are supported.
The service ceiling applies to the current group; tighter visible ancestor
limits also constrain headroom. Outside a readable Linux memory cgroup, the
fallback sums the process and descendant RSS; shared pages may be counted more
than once. If Linux memory accounting is unavailable, monitoring is disabled.
Setting `SCRAPEYARD_WORKERS_MEMORY_LIMIT_MB=0` explicitly disables these checks.

Each browser target waits for its concurrency permit and for memory headroom
greater than `(active_browser_targets + 1) * SCRAPEYARD_WORKERS_BROWSER_MEMORY_RESERVE_MB`.
The reservation lasts through that target, including pagination and retries;
waiting consumes the existing run budget and remains cancellable. The reserve
is additional to measured usage, intentionally covering concurrent launches
before their memory becomes visible. Tune it from browser peaks. It cannot stop
an already-running browser from growing; retain the hard container limits.

`scrapeyard_admission_rejections_total{reason="backlog|memory"}` counts rejected
new runs. `scrapeyard_memory_headroom_bytes` reports sampled headroom before
browser reservations (infinity when unmonitored), and
`scrapeyard_memory_deferrals_total{stage="worker|browser"}` counts blocked start
checks. Existing active-work and queue-age metrics show occupancy and delay.

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

`/health/ready` exposes `workers.queue_depths` with the exact keys `high`, `normal`,
and `low`. Each value is a direct `ZCARD` of its known intake queue and includes
waiting/deferred members, including an orphaned member until the next admission
attempt removes it, but excludes admitted base-queue and in-progress work.
`active_tasks` remains
the separate running-handler count. If Redis cannot provide these bounded
depth reads, all three values are `null`, the Redis dependency is unhealthy,
and `/health/ready` returns 503.

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

Startup reconciles queued SQLite ownership after Redis connects and before
APScheduler starts, and the same repair runs periodically in bounded batches.
Only queued rows older than
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
Priority admission atomically discards a sorted-set member whose arq payload
has expired instead of moving an unexecutable delivery to the worker queue.
The periodic pass then restores the accepted run with its original run ID.

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

Selector, validation, and application DNS work use a dedicated executor with
`SCRAPEYARD_RUN_THREAD_MAX_WORKERS` slots. A run may become terminal at its
deadline because Python cannot forcibly stop a running thread, but its slot is
retained until the underlying work actually finishes. This contains lingering
work outside the process-wide executor and prevents timed-out runs from
releasing internal capacity early. Monitor active and post-deadline work with
`scrapeyard_active_work{kind="run_threads"}` and
`scrapeyard_active_work{kind="lingering_run_threads"}`.

Production `basic` requests use an asynchronous streaming transport. The
fetched-byte ceiling counts decoded body chunks as they are read, including
redirect and application-level retry responses against one shared run budget.
A single valid `Content-Length` is checked before reading as an optimization,
but decoded chunks remain authoritative for compressed, chunked, missing, or
misleading lengths. Crossing the byte ceiling or monotonic run deadline closes
and awaits the socket task before the structured budget failure is returned;
only a bounded body is handed to Scrapling's parser. The Compose admission
threshold is 3072 MiB under a 4 GiB cgroup ceiling, leaving an explicit 1 GiB
reserve for active response bodies, browser processes, serialization, SQLite,
and interpreter overhead. Browser-backed
`dynamic` and `stealthy` fetches do not expose reliable total transfer size and
are therefore excluded. Their stored debug excerpts and screenshots are
instead bounded by `SCRAPEYARD_RUN_MAX_BROWSER_DEBUG_BYTES`; omitted or
truncated captures are described in the result diagnostics without failing the
run. Result JSON is measured using the exact compact UTF-8 representation and
is written by temporary file plus atomic replacement before metadata commit.

### Ad-hoc submission idempotency

`POST /scrape` optionally accepts one `Idempotency-Key` header of 1–128 visible
ASCII bytes. `jobs.db` stores only its SHA-256 digest, scoped by the validated
API-key digest. When API authentication is disabled, the scope is the single
literal `local-development` identity; local clients must therefore coordinate key
uniqueness. The raw YAML SHA-256, job ID, run ID, derived sync/async response
mode, creation time, and expiry are inserted in the same immediate SQLite
transaction as the job. A matching retry cannot enqueue again; different YAML
returns `409`.

Async submissions preserve their original `202` acceptance contract. Sync
retries wait on the persisted job up to the normal sync timeout and return
`200` when terminal, otherwise `202` with the current persisted status. Every
replay has `Idempotency-Replayed: true`, and both accepted response forms
include the original `job_id` and `run_id`. Omitting the header retains
non-idempotent behavior.

Records expire after `SCRAPEYARD_IDEMPOTENCY_RETENTION_HOURS` (24 hours by
default). The first reuse after expiry removes that caller/key record lazily
and creates a new submission. The cleanup loop additionally deletes expired
rows in transactions of at most `SCRAPEYARD_IDEMPOTENCY_CLEANUP_BATCH_SIZE`.
Automated
ad-hoc history retention excludes jobs with a future expiry and rechecks that
condition in its final write transaction, so independently configured history
retention cannot shorten the replay window. Explicit job deletion and
failed-enqueue rollback retain their documented behavior and remove associated
records by foreign-key cascade.
A crash after the SQLite transaction but before Redis acceptance leaves the
persisted queued run for the existing stale-queue reconciler; a retry never
risks a second enqueue merely to close that cross-system window.

### Result artifact reconciliation

`results_meta` is authoritative for retained result ownership. A valid row's
contained `file_path` names exactly one `project/job/run` directory below
`SCRAPEYARD_STORAGE_RESULTS_DIR`; that directory must contain a regular,
non-symlink `results.json` holding valid UTF-8 JSON. The parent job is not part
of this ownership decision. Deleting a job with `delete_results=false` leaves
its metadata and artifact authoritative and eligible for normal age/per-job
retention, not orphan removal.

The result API uses this stored project to authorize project-scoped credentials
after the parent job has been intentionally removed. Both explicit run lookup
and deterministic newest-result lookup authorize the metadata row before the
artifact is opened; missing and cross-project retained IDs return the same
`404` response.

The periodic cleanup order is expired-result retention, per-job result
pruning, artifact reconciliation, expired submission-idempotency deletion,
then webhook tombstone scrubbing. Retention drains repeated deterministic
batches in one maintenance cycle. Artifact reconciliation likewise drains
independent metadata and filesystem cursor pages until each scan is exhausted
or its cycle ceiling is reached. Each transaction/page remains bounded by its
category batch setting, while
`SCRAPEYARD_STORAGE_CLEANUP_CYCLE_MAX_ITEMS_PER_PHASE` bounds completed items
per phase. `SCRAPEYARD_STORAGE_CLEANUP_CYCLE_MAX_SECONDS` is carried into
artifact reads and tree walks as an absolute monotonic deadline. No new
metadata row, run candidate, or recursive directory chunk is admitted after
that deadline. An already admitted bounded filesystem call or candidate is
allowed to finish so mutation never continues after cleanup returns. If either
budget is reached, the loop waits only
`SCRAPEYARD_STORAGE_CLEANUP_CATCHUP_DELAY_SECONDS` before continuing instead
of sleeping for the normal six-hour interval. It commits each metadata batch
and releases SQLite before removing
the corresponding directories, so filesystem latency does not block result
metadata access. Retention remains metadata-first, so a crash leaves a
recoverable filesystem orphan.
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
Metadata validation reads at most `SCRAPEYARD_RUN_MAX_SERIALIZED_RESULT_BYTES`;
larger files are reported as corrupt without being loaded. A run-tree snapshot
is bounded by twice the sum of the configured serialized-result and
browser-debug byte limits (covering both committed targets and interrupted
atomic-writer temp files) and by
`SCRAPEYARD_STORAGE_RECONCILIATION_MAX_ENTRIES_PER_RUN`. Exceeding either tree
bound records a typed scan failure and leaves the directory untouched. The
tree entry ceiling does not limit project, job, or run namespace fanout. Those
directories use lexicographic cursor pages, keeping only a batch-sized selection
of names plus lookahead in memory. Empty and malformed parents also consume the
filesystem page budget, so they cannot prevent progress to later runs. Selecting
a page still enumerates the parent directory's names, with cooperative deadline
checks; allow enough cleanup time for that enumeration on very large directories.
Destructive reconciliation opportunistically removes empty job/project parents
with non-following `rmdir` calls, including parents left by retention. Nonempty
or racing parents remain for a later pass; the results root is never removed.
These empty-parent removals do not count as removed run artifacts.

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

`SCRAPEYARD_STORAGE_RECONCILIATION_DRY_RUN=true` is the safe default. Each page
validates and scans cursor-paginated work no larger than
`SCRAPEYARD_STORAGE_CLEANUP_BATCH_SIZE`, performs the same eligibility checks,
and reports what would be removed without mutating files or metadata. The two
scans expose exhaustion independently, and their keyset cursors are committed
to `results_meta.db` after each page only through the completed prefix. A
deadline reached inside a page leaves later rows and candidates at the durable
cursor for the catch-up pass. Process restarts therefore resume later entries
without skipping unfinished work. Large stores are covered without one
unbounded metadata load or full-tree candidate list. Review at least one
grace-period window of summaries
before setting dry-run to `false`. Destructive passes
remove only eligible directories and known temp files, converge idempotently,
and never modify `results_meta`.

Every cycle logs a typed summary: metadata inspected/valid, missing/corrupt/
unreadable/unsafe artifacts, filesystem entries/runs inspected, independent
scan exhaustion and remaining-work state, orphan/temp candidates,
recent/active/race skips, proposed and completed removals, removed regular-file
bytes, dry-run state, and failure count. Per-entry reports contain only bounded
job/run or relative identifiers and exception types, never JSON contents,
stored YAML, secrets, raw exception text, or outside paths. Missing files,
invalid JSON, I/O failures, unsafe paths, symlinks, and non-regular result files
remain in metadata for operator diagnosis or the original explicit-deletion
retry. A top-level reconciliation exception is isolated so webhook scrubbing
can continue; cancellation propagates, and failures retry on the next six-hour
pass.

Scanning visits retained metadata and files below the results root; namespace
names are enumerated again when selecting subsequent pages. JSON parsing,
directory walks, non-following size accounting, and removal
run in worker threads. Allow I/O headroom and monitor cleanup duration and disk
capacity on large trees. There is no content repair, metadata synthesis,
filesystem-to-metadata recovery, or cross-instance reconciliation lease.
Filesystem mtimes can be coarse or administratively changed; permission
changes can make a candidate unverifiable; a path can still change after its
last check; and SQLite/filesystem operations are not one transaction. Active
and age rechecks reduce but cannot eliminate that gap. The service retains its
documented single-process/local-filesystem assumption.

Size cleanup so each category's daily item budget exceeds the measured rate at
which rows become eligible, with recovery headroom after the longest planned
downtime. With the defaults, one cycle may process 10,000 eligible items per
category and saturated cycles repeat after five seconds. Track both
`scrapeyard_cleanup_eligible_items` and
`scrapeyard_cleanup_oldest_eligible_age_seconds`; a rising count or age proves
retention is falling behind even while individual cycles succeed.

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

Each attempt resolves and validates the destination once, pins the socket URL
to that public address, and retains the logical hostname for `Host` and TLS
SNI. HTTP connection pools are isolated by that logical hostname, so hosts
sharing one address can never reuse each other's authenticated TLS session.
Resolver availability failures are retryable transport failures; a concrete
private or otherwise non-public address remains a permanent safety rejection.
Response handling streams only status and headers, closes the response without
consuming its unused body, and therefore does not buffer destination-controlled
response content.

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
leaves the durable row pending for restart. Shutdown stops the coordinator and
drains only the bounded queued/in-flight set within one shared monotonic hard
deadline, then cancels unfinished requests. Cancellation-resistant work cannot
extend the bounded wait beyond that deadline: it remains tracked and the
runtime reports shutdown failure instead of silently abandoning it or starting
a second dispatcher over it. A request may have reached the receiver even when
success was not durably recorded; that row stays pending
with its reserved attempt and restart may send the same `delivery_id` again.
Receivers must therefore deduplicate. The contract remains at-least-once, not
exactly-once.

### Dead-letter retention and secret scrubbing

Failed rows remain fully inspectable for
`SCRAPEYARD_WEBHOOK_FAILED_RETENTION_DAYS`; delivered rows retain their full
request data for `SCRAPEYARD_WEBHOOK_DELIVERED_RETENTION_DAYS`. The existing
periodic cleanup loop drains deterministic bounded transactions and scrubs the
URL, headers, payload, timeout, and free-form last error in place. Pending rows
are never selected by terminal retention settings. A cleanup failure is logged
and retried on a later pass without stopping workers or changing scrape state.

Scrubbing deliberately keeps a minimal tombstone: `delivery_id`, `job_id`,
`run_id`, event, terminal status/reason, attempts, and lifecycle timestamps.
Item-05 reconciliation treats that tombstone as existing durable intent. It
must not be deleted independently while its terminal run remains: removing the
only logical job/run/event evidence would make reconciliation recreate and
redeliver an old terminal event. After
`SCRAPEYARD_HISTORY_WEBHOOK_TOMBSTONE_RETENTION_DAYS`, cleanup may delete an old
scheduled run and all of its terminal tombstones in the same `jobs.db`
transaction. With the run absent, startup reconciliation has no event to
recreate. Old terminal ad-hoc jobs use the same tombstone eligibility rule and
the persisted, resumable job-deletion lifecycle. Scrubbed rows cannot
transition back to pending and are never selected for dispatch.

### Durable history retention

Result retention and operational-history retention are intentionally separate.
An ad-hoc job can be removed while `results_meta.db` and its artifact remain
readable and authorized from retained project metadata. Cleanup never reserves
queued/running jobs and never reserves a job with pending webhook work. It
marks an eligible ad-hoc job `deleting`, removes `errors.db` rows in bounded
batches, and only then removes its runs, terminal tombstones, idempotency rows,
and parent in one `jobs.db` transaction. A crash or phase failure leaves the
reservation visible for the next pass.

Scheduled parents remain. Cleanup selects only terminal, non-current,
webhook-reconciled runs beyond the configured age or retained-count window.
It removes run errors first, then atomically rechecks eligibility and deletes
the run plus its scrubbed tombstones. API `run_count` is a lifetime counter and
`last_run_at` is a lifetime summary maintained on `jobs`; detailed history
compaction therefore does not change either value or require a full-history
aggregation for `GET /jobs`.

At startup Scrapeyard first repairs stale running state, then scans terminal
runs in deterministic retry/completion/job/run order. Every accepted run owns
an encrypted immutable `job_runs.config_yaml` snapshot whose SHA-256 must match
the run's persisted configuration hash; webhook applicability is parsed from
that snapshot rather than the mutable scheduled parent. Legacy rows are
backfilled only when the current parent hash proves it is the same config.
Each repair transaction rechecks the exact terminal/config snapshot, converges
a still-current parent left as `running`, and inserts a missing deterministic
intent. A config parse or hash mismatch fails closed rather than silently
guessing, but the candidate receives a durable retry timestamp and processing
continues through the batch. Untouched candidates sort ahead on the next
bounded pass, so one damaged row cannot indefinitely block later repair.
Repaired rows
are committed before webhook dispatcher startup reads and schedules pending
deliveries. Redis then connects, stale queued deliveries are reconciled, and
only then does APScheduler start. Scheduler-triggered stale-running recovery
also invokes terminal-intent repair before replacing the run.

Result data has unavoidable cross-system boundaries. `results.json` is written
on the filesystem before `results_meta.db` commits, and both precede the
terminal `jobs.db` transaction. A normal metadata-commit failure restores the
previous same-run artifact (or removes a newly created artifact). A process
crash can still leave an orphaned result
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

Use a least-privilege `health-detail` credential for `/health/ready` and
`/metrics`. The complete signal inventory, bounded-label contract, scrape
configuration, and suggested saturation/backlog/disk/background-task alerts
are in [MONITORING.md](MONITORING.md). Also monitor container restarts, OOM
kills, volume inode exhaustion, and host/network policy outside the process.

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

Unauthenticated protected requests should fail when `SCRAPEYARD_API_CREDENTIALS` is
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

Detailed monitoring requires its own least-privilege credential:

```bash
curl -fsS \
  -H "X-API-Key: $SCRAPEYARD_MONITOR_API_KEY" \
  http://127.0.0.1:8420/health/ready
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

- `SCRAPEYARD_API_CREDENTIALS` is set, least-privileged, and stored as a secret.
- Scrapeyard HTTP is reachable only from Eyebox and internal monitoring.
- Redis is not exposed outside the private runtime network.
- Egress policy blocks metadata and private/internal networks.
- `/data` is persistent and has disk alerts.
- Database and result-artifact restore has been tested.
- Full test suite passes in CI.
- Staging has run representative Eyebox scrapes, unreachable targets, bad
  configs, and a restart during queued/running work.
