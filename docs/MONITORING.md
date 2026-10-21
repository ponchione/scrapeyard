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
  Requests refused for the host they named (`SCRAPEYARD_ALLOWED_HOSTS`) never
  reach a route; `scrapeyard_host_refusals_total` counts them.
- Queue depth and oldest waiting age use only `high`, `normal`, and `low`.
- Active jobs, targets, browser targets, bounded run threads, and result-response
  threads are compared with process capacity. Lingering threads separately show work still
  consuming a slot after its owning run reached a terminal deadline.
- Run and target count/latency use bounded status, trigger, and fetcher labels.
- Retry, rate-limit wait, extracted-record, and serialized-byte totals show
  work amplification and output pressure.
- Durable webhook status counts and oldest-pending age expose retry backlog.
- Cleanup pass/item/removed-byte totals, artifact finding counts, durable
  history phase failures, exact eligible backlog count/oldest age, and
  worker/scheduler/cleanup/webhook/reconciliation last-success timestamps
  expose stalled maintenance and retained-data integrity. The worker's
  timestamp advances on every completed queue poll.
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

`/health/ready` applies an explicit short timeout to Redis, queue, every SQLite
database (`jobs.db`, `errors.db`, and `results_meta.db`), disk, and a one-byte
atomic create/read/remove operation in the result directory. It also fails when
the worker runner, APScheduler, cleanup task, webhook coordinator/worker set, or
either interval-aware reconciliation service has stopped or gone too long without
a successful pass. Optional project summaries use the same configured timeout,
retain the last successful cache on refresh failure, and make readiness return
`503` with an explicit detail when a requested refresh fails. Exhausted worker
capacity returns a degraded `200`;
insufficient free disk or any other failed required dependency/background task
returns `503`. Liveness never performs these operations.

The container's own health check asks readiness, not liveness: Compose and
the image run `python -m scrapeyard.runtime.healthcheck`, which calls
`/health/ready` from inside the container with a `health-detail` credential
taken from the container's settings and is healthy on a `200`. Of several
credentials that hold the scope it takes the one with the fewest scopes, so
give monitoring a credential of its own: readiness is rate-limited by
credential, and a `429` is unhealthy like any other answer. `docker ps`
shows `unhealthy` after three failed checks, 30 seconds apart. A deployment
with no `health-detail` credential is checked for liveness, as before.
Docker does not restart an unhealthy container; the status is for an
operator or an alert to read. One cause does not wait for that: when the
embedded worker ends outside a shutdown, as it does when Redis goes away
under it, the process ends itself with a `CRITICAL` line saying so, and the
container's restart policy starts it again through a full start. A worker
that has finished no poll of the queue for 60 seconds, as when Redis stops
answering without closing its connections (a paused container, or a path
that drops packets), counts as ended: readiness names it `stalled` and the
process ends the same way, 75 to 90 seconds after its last poll. Runs under
way get the shutdown grace to finish and are then cancelled, as at any stop.
Until Redis is back each start fails, and Docker keeps trying, at most a
minute apart. A shutdown that has not finished 10 seconds after the shutdown
grace is ended with exit status 70. The next start releases the queue
deliveries the ended process could not mark finished (a `WARNING` line counts
them), so they do not hold the worker's slots until their keys expire.

A start waits on the same check. `docker compose up --wait` succeeds only
for an application that is ready, so the deploy wrapper now fails a start
whose application runs and stays unready, and leaves it stopped; see
[DEPLOYMENT.md](DEPLOYMENT.md#retained-release-artifacts-and-rollback).

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

## The host check

`security/health-check.py`, installed as `/usr/local/sbin/scrapeyard-health`
and run every five minutes by `scrapeyard-health.timer`, looks at the host
from outside the application and says what it found. It needs no service to
run; what it reports through is a hook the operator installs.

| Check | Fails when |
| --- | --- |
| `deployment` | Docker did not list or describe the deployment's containers, or `docker.service` is stopped, failed or stopping. Then Docker is not asked at all: its socket would start a stopped Docker for the first client, and the boot service, which Docker wants, would start the application with it in the middle of maintenance. |
| `application` | The application's container is not running, or its readiness is not `200`. Readiness is asked from inside the container, with the credential its own health check takes (liveness, from a release older than that check); the line names what failed (`redis`, `worker`, ...) and what Docker says. Not judged while a backup has it stopped, or the boot service's start or stop or a promotion is under way, and for ten minutes at most; between the retries of a start that keeps failing it is judged. |
| `disk` | `/`, `/var/lib/docker` or `/var/backups/scrapeyard` has less than 10% or 2 GiB free, or less than 10% of its inodes. |
| `backup` | The last backup completed more than 26 hours ago or more than 15 minutes in the future (made while the clock was ahead, it would read as fresh until the clock passed it), none has, its status file cannot be read, or `scrapeyard-backup.timer` is not active. Whether archives leave the host is said, not judged. |
| `policy-watch` | `scrapeyard-policy-watch.timer` is not active, or the watch wrote to its record since the last check (once, with the lines). The record's first line counts too. Without a status file to compare with, lines from the last 15 minutes count as new. |
| `units` | Any unit on the host has failed, the check's own aside. |
| `restarts` | Since the last check, an application, Redis or probe container was restarted (TD-89's ending included), or `scrapeyard.service` or the tunnel's unit was. Once, with the counts. |
| `tunnel` | `cloudflared-scrapeyard.service` exists and is not active, or, given `--tunnel-ready-url`, the connector's own readiness is not `200`. |
| `edge` | Given `--edge-url`: the public `/health`, asked through Cloudflare, Access and the tunnel with the service token in `/etc/scrapeyard/edge-access.json`, does not answer `200` with status `ok`, or the token file is not root's alone (mode 600). A 502 from a connector with no connection, a 401 from a revoked or expired token and a redirect all fail. Not judged while the application is stopped on purpose, as for `application`. |
| `expiry` | Given `--expires LABEL=YYYY-MM-DD` (repeatable): the first of those days is `--expiry-warn-days` (30) or fewer days away, or has passed, or an entry is not in that form. For credentials whose expiry nothing on the host can read, such as an R2 key or an Access service token. After renewing one, change its date in the unit's drop-in. |
| `hooks` | A hook is installed that root cannot trust, or a ping line is not one `https` URL with no login, space or comment; nothing is sent through it. |
| `previous-run` | The run before this one ended before it reported (once). |
| `delivery` | A warning, not a failure: the last run could not hand the report to one of the hooks (by its kind and host, or the command's exit status). It is said through the others, and the state stays as it was. ntfy hears of changes of state only, so with ntfy alone a dead topic shows in the journal and nowhere else; in the status file the line has `"ok": false` and `"warning": true`. |

Each run prints one line for each check to the journal
(`journalctl -u scrapeyard-health`) and writes them to
`/var/lib/scrapeyard/health-status.json`. Then it hands the report to what is
installed, and says what it did:

- `/etc/scrapeyard/health-ping`, root's alone (mode 600), one service a line:
  - `healthchecks https://hc-ping.com/<uuid>`: every run, the URL when every
    check passes and `<url>/fail` when one does not, with the report as the
    body. A run that never happens (the host down, the timer stopped, the
    script broken) is what healthchecks.io itself notices: no ping arrives
    within the check's grace.
  - `ntfy https://ntfy.sh/<topic>`: a message when the state changes, and
    every six hours while it is failing. A topic on ntfy.sh is readable by
    anyone who knows its name: pick a long random one.
- `/etc/scrapeyard/health-hook`, an executable of root's that no one else
  can change: run every time as `health-hook ok|fail changed|unchanged`, with
  the report on standard input and a 30-second limit. Only its exit status is
  logged.

With neither installed, it says what it would have sent. `scrapeyard-health
--dry-run` runs the checks and says what each hook would be sent, sending
nothing. The URLs are credentials of their own and are never printed, nor
followed to where a redirect points; the report holds names, states, ages
and sizes, no setting and no credential. It calls the host `Scrapeyard host`,
or what `--label` says, and never by its host name, which can name its
address. A run that fails says only what kind of error ended it, and the next
run reports that once; a run that never happens is noticed only by
healthchecks.io, whose missed pings are the one alarm that needs nothing on
the host to work.

The tunnel's connector serves its own readiness on loopback
(`http://127.0.0.1:20241/ready` by default), but the connector unit in
`security/systemd/` drops every packet from loopback (`IPAddressDeny`), so the
host cannot read it. Without `--edge-url` the check judges the connector by
its unit alone. With it, the check asks the application's `/health` the way
Eyebox does, through the public name, so a connector that runs and has no
connection, a broken route or DNS record, and an Access policy that no longer
admits the token all fail it. Give the check its own Access service token, in
a policy of its own on the application, so that it can be revoked without
touching Eyebox's:

```bash
# {"client_id": "...", "client_secret": "..."} of the monitor's service token
sudo install -m 600 /dev/stdin /etc/scrapeyard/edge-access.json < edge-access.json
sudo install -d /etc/systemd/system/scrapeyard-health.service.d
printf '[Service]\nExecStart=\nExecStart=/usr/local/sbin/scrapeyard-health --edge-url https://<public name>/health\n' |
  sudo tee /etc/systemd/system/scrapeyard-health.service.d/edge.conf
sudo systemctl daemon-reload
sudo scrapeyard-health --dry-run --edge-url https://<public name>/health
```

The service token expires (after a year by default); the check fails with
401 from then on. Rotate it as the consumer's is rotated
([DEPLOYMENT.md](DEPLOYMENT.md)).

Install it from a release:

```bash
sudo install -m 755 "$RELEASE/source/security/health-check.py" /usr/local/sbin/scrapeyard-health
sudo install -m 644 "$RELEASE/source/security/systemd/scrapeyard-health.service" \
  "$RELEASE/source/security/systemd/scrapeyard-health.timer" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now scrapeyard-health.timer
sudo scrapeyard-health --dry-run
```

### On the first production host

State on 2026-10-21. Three things run different code, so each is named:

- **Deployed application image:** release `e31e6a3`
  (`org.opencontainers.image.revision`), promoted 2026-10-20 and linked from
  `/srv/scrapeyard-releases/current`.
- **Installed host scripts:** `/usr/local/sbin/scrapeyard-backup` and
  `scrapeyard-release` are byte for byte that release's `security/backup.sh`
  and `security/manage-release.sh`. `/usr/local/sbin/scrapeyard-health` is
  newer: `security/health-check.py` with `--edge-url` (pull request #46) and
  `--expires` (pull request #48), which no release holds yet, installed by
  hand; the copy from before both is `/root/scrapeyard-health.before-edge`.
  `/etc/scrapeyard/backup-upload` is `security/backup-upload-r2.sh` and
  `/usr/local/sbin/scrapeyard-release-upload` is `security/upload-release-r2.sh`
  ([DEPLOYMENT.md](DEPLOYMENT.md#the-first-production-hosts-store)).
- **Repository `main`:** carries all of the above, and this record.

What is installed, and what was seen:

| What | Installed | Verified |
| --- | --- | --- |
| Public-path check | Drop-in `/etc/systemd/system/scrapeyard-health.service.d/edge.conf` runs `scrapeyard-health --edge-url https://scrapeyard.eyebox.io/health` and the `--expires` below. | 2026-10-21 12:52 UTC: all 9 checks passed, `edge: the public name answered HTTP 200 through Access and the tunnel, status ok`. From the host, `/health` without a token answers 401. |
| Monitor's Access token | Service token `scrapeyard-monitor-2026-10` (expires 2027-10-21), in `/etc/scrapeyard/edge-access.json`, root:root, mode 600. It is admitted by its own policy on the Access application `Scrapeyard API`, `Scrapeyard host monitor service token only` (decision Service Auth, precedence 2). Eyebox's token `eyebox-ingest-2026-10` and its policy (precedence 1) were not changed. | The token reached the host encrypted to a one-time key made on the host, and was never printed. Revoke it, or delete its policy, without touching Eyebox. |
| healthchecks.io | `/etc/scrapeyard/health-ping`, root:root, mode 600, one `healthchecks` line for the check `scrapeyard-host`. The URL is the credential and is kept nowhere else on the host, not in the journal and not in this repository. | 2026-10-21 13:04 UTC: a run passed all 9 checks and hc-ping.com answered `HTTP 200`. |
| Credential expiry | The same drop-in: `--expires r2-backup-upload=2027-10-21 --expires access-monitor=2027-10-21 --expires access-eyebox=2027-10-12`. `expiry` fails from 30 days before the first (`--expiry-warn-days`), so healthchecks.io alerts until it is renewed and the date changed here. | 2026-10-21 13:46 UTC: `ok expiry: 3 recorded; the first, access-eyebox 2027-10-12, in 356 days`, 10 of 10 checks. |
| Cloudflare notifications | Email policies: `Scrapeyard tunnel health` (the tunnel `scrapeyard-prod`, since 2026-10-15), `Expiring Service Token Alert` and `Leaked Service Token Alert` (every Access service token), and the auto-created `Default budget alert` at $10. | Cloudflare's alert history shows tunnel health emails sent on 2026-10-17 (06:39 UTC) and 2026-10-21 (06:18 UTC, when unattended upgrades restarted `cloudflared`). Receipt in the operator's mailbox was not seen. |

The check `scrapeyard-host` is meant to have the schedule type *Simple*, a
**5-minute period** (the timer's) and a **10-minute grace**, with an email
integration. **Not verified:** that the dashboard has those settings, that
it shows the check *up*, and that a failure or a missed ping reaches the
operator's mail. An `HTTP 200` from hc-ping.com says the ping was taken, not
how the check is configured. A read-only API key of the project (Project
Settings → API Access) would let the settings and last ping be read; none
exists on the operator's machine.

Planned stops should not alert. While a backup has the application stopped
(about 30 s from 04:40 UTC), or the boot service starts it after the
unattended-upgrade reboot at 08:00 UTC, `application` and `edge` say "not
judged" and the ping stays a success; the grace covers runs missed while the
host reboots. A failing check sends `/fail` and alerts at once; a host that
is down, or a timer that stopped, alerts after period plus grace.

A real failure has reached healthchecks.io: at 13:19:58 UTC on 2026-10-21
a run failing on `fwupd-refresh.service` sent `/fail` (`HTTP 200`), and the
run at 13:20:30 sent success. If the check is set up as above, that was a
*down* and an *up* email; whether they arrived was not seen.

**`fwupd-refresh.timer` is disabled (2026-10-21).** `fwupd-refresh.service`
failed at 12:00, 12:53 and 13:06 UTC: `fwupdmgr refresh` times out reaching
`cdn.fwupd.org`, which the host could not reach over IPv4 or IPv6
(`fwupd.org` and other sites answered at once), and `units` fails while it
is failed. The VM's only firmware device is its virtual CPU, so the refresh
had nothing to update. The change, on the host:

```bash
sudo systemctl disable --now fwupd-refresh.timer
sudo systemctl reset-failed fwupd-refresh.service
```

Package updates are untouched: `apt-daily.timer`, `apt-daily-upgrade.timer`
and `unattended-upgrades.service` stay enabled. To reverse it:
`sudo systemctl enable --now fwupd-refresh.timer`. Debian's packaging is
meant to keep a timer the administrator disabled disabled when `fwupd` is
upgraded; that was not tested, so check `systemctl is-enabled
fwupd-refresh.timer` after an upgrade of `fwupd` or of the distribution.

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
