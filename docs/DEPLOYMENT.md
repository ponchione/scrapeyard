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

- Do not publish `8420` on a public interface.
- For local Docker Compose, keep the existing bind:

  ```yaml
  ports:
    - "127.0.0.1:8420:8420"
  ```

- For a shared deployment with Eyebox, prefer a private service network over a
  host port. If a reverse proxy is used, restrict the proxy route to Eyebox and
  keep `/health` available only to internal monitoring.
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
- `SCRAPEYARD_MAX_REQUEST_BYTES`
- `SCRAPEYARD_STORAGE_RETENTION_DAYS`
- `SCRAPEYARD_STORAGE_MAX_RESULTS_PER_JOB`
- `SCRAPEYARD_HEALTH_DISK_FREE_MIN_MB`

Keep browser concurrency lower than total job concurrency. Browser jobs are the
most memory-intensive path.

## Monitoring

Monitor:

- `/health` status and dependency details
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

