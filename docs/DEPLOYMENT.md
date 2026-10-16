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
- Scrapeyard runs under an init process as PID 1 (`init: true` in
  `docker-compose.yml`; `docker run --init` or an equivalent elsewhere). The
  image's entrypoint execs Uvicorn, which never reaps processes it did not
  start, and browser runs leave orphaned Chromium and crashpad children that
  are re-parented to PID 1. Without an init they stay as zombies: one
  development container without an init accumulated 7,468 in 16 hours, enough
  to exhaust the Compose `pids_limit` of 512 many times over.
- Browser scraping egress is controlled by the host, orchestrator, proxy
  gateway, or firewall policy and attested before the application starts.

## Container runtime versions

A host installs these from Docker's package repository and holds them
(`apt-mark hold`), so an unattended upgrade cannot move the host past what
was rehearsed:

| Package | Version |
| --- | --- |
| `docker-ce`, `docker-ce-cli` | 29.9.0 |
| `containerd.io` | 2.4.1, which carries runc 1.5.2 |
| `docker-compose-plugin` | 5.6.0 |

A host also keeps Docker's classic image store (`docker info` names the
storage driver `overlay2`). A release names its images by identifier, and an
image has another identifier under containerd's image store. Loaded there, a
release built on the classic store is refused at the load, before the
running application is stopped, and so is the reverse. An upgrade in place
keeps the store a host has. A new installation of Docker 29 or later chooses
containerd's unless `/etc/docker/daemon.json` says otherwise before Docker
first starts:

```json
{ "features": { "containerd-snapshotter": false } }
```

The container smoke lane and the release rehearsal install the same versions
and select the same image store before they build anything
(`scripts/install_container_runtime.sh`), and fail when the engine,
containerd, runc, Compose or image store that then runs is another one.
The rehearsal records them in `container-runtime.txt`. A runner image's own
Docker is not a stand-in: a release that had passed every lane on the
runner's runc started no container under the host's
([seccomp allowlist](#container-privilege-and-filesystem-boundary)).

Raise the versions in that script and in this table in one change, let the
lanes pass on it, and only then upgrade and reboot the host. A held package
takes no fixes, security fixes included, so do not leave the hold in place
for long.

## Host baseline

What the first production host was given besides Docker and the release, so
that a lost host can be rebuilt from this guide:

- Ubuntu 24.04 with every update applied, and the release's Docker versions
  ([above](#container-runtime-versions)), held.
- SSH by key only: `PasswordAuthentication no`,
  `KbdInteractiveAuthentication no` and `PermitRootLogin no` in a file under
  `/etc/ssh/sshd_config.d/`. Test a second login before closing the first.
- An inbound firewall that admits SSH and nothing else (`ufw default deny
  incoming`, `ufw allow 22/tcp`). Compose publishes no port. A default `ufw`
  leaves the egress policy's `INPUT` jump alone ([Egress](#egress)); the
  provider's own firewall, where there is one, may not filter traffic from
  its other customers.
- Unattended security updates, with `Unattended-Upgrade::Automatic-Reboot`
  on at a fixed time of day. Nobody logs in to restart the host, and the
  [boot service](#boot-service-and-promotion) brings Scrapeyard back. A run
  in flight at that time is interrupted, so keep the consumer's schedule
  clear of it.
- Docker's `json-file` logs rotated in `/etc/docker/daemon.json`
  (`max-size` 20m, `max-file` 5); unrotated, a busy container fills the disk.
- No account in the `docker` group. Its members are root in all but name;
  operate through `sudo`.

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

### Ingress through a tunnel

When the consumer runs with another provider, ingress can be a tunnel that
the host opens outwards instead of a reverse proxy. The host then accepts no
inbound connection except SSH, and there is no published port, certificate
or list of the consumer's addresses to maintain. The first production host
uses a Cloudflare Tunnel behind a Cloudflare Access service token. The
provider ends TLS at its edge, so it sees the API key and every request and
result in clear; choose this only where that is acceptable.

Set it up in this order:

1. **Access application** for the public name, with one policy of the
   service-auth kind that includes a single service token and nothing else.
   Turn on the 401 answer for service-token requests, so a caller without
   the token is refused and not sent to a login page. Note the application's
   audience tag.
2. **Tunnel**, with the routes in
   [`security/cloudflared/ingress.example.yml`](../security/cloudflared/ingress.example.yml):
   the public name, the paths Eyebox calls, the application's fixed address
   on the deployment bridge, the Access check repeated by the connector, and
   404 for everything else. Then a proxied DNS record for the name that
   points at the tunnel.
3. **Connector on the host.** Install `cloudflared` from Cloudflare's package
   repository and add that origin to the unattended upgrades
   (`"origin=cloudflared,label=cloudflared";` in
   `Unattended-Upgrade::Origins-Pattern`): the connector is the only thing
   between the edge and the application. Then:

   ```bash
   sudo useradd --system --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin cloudflared
   sudo install -d -m 700 /etc/cloudflared
   sudo install -m 600 /dev/null /etc/cloudflared/tunnel-token   # then put the tunnel's token in it
   sudo install -m 644 security/systemd/cloudflared-scrapeyard.service /etc/systemd/system/
   # This host's own public addresses, which differ on every host:
   sudo install -d /etc/systemd/system/cloudflared-scrapeyard.service.d
   printf '[Service]\nIPAddressDeny=%s\n' '203.0.113.7/32 2001:db8::7/128' |
     sudo tee /etc/systemd/system/cloudflared-scrapeyard.service.d/host-addresses.conf
   sudo systemctl daemon-reload
   sudo systemctl enable --now cloudflared-scrapeyard.service
   ```

   The unit gives the connector no capability and no view of Docker, the
   settings file or the releases. Whatever the tunnel's routes say, it can
   connect to the application's address, the local resolver and the public
   internet only: Redis, the probe, loopback, the host's own addresses and
   private ranges are dropped.
4. **The consumer** sends the service token's id and secret as
   `CF-Access-Client-Id` and `CF-Access-Client-Secret` beside its
   `X-API-Key`. Give it a credential limited to `submit` and `read` in its
   own project ([Secrets](#secrets)).

A route matches a path and never a method. `PUT` and `DELETE /jobs/{id}`
therefore reach the application, and it is the credential's scopes that
refuse them. Do not give the consumer's credential more than it needs on the
strength of the route list.

Check all of this after the setup and after any change to it, from outside:

- Without the token, and with a wrong id or secret, Access answers 401.
- With it, `/health` answers 200 and each route in the list reaches the
  application, which then asks for its own key.
- Readiness, metrics, the job list, pause and resume, the domain guards, and
  odd spellings of an allowed path (dot segments, an encoded or doubled
  slash, upper case) are answered by the connector or the edge, not by the
  application.
- The consumer's key is refused on `PUT` and `DELETE /jobs/{id}`.
- A submission is found again through `/scrape/acceptance`, polled and read.
- The host's own address answers on no port but SSH.

And from the host, as the connector's unit (`systemd-run -p` with the same
address properties), a connection to Redis, to the probe, to loopback and to
the host's own addresses must time out.

The edge may refuse a client before Access reads its token. The zone's
Browser Integrity Check answers 403 to the default signature of Python's
`urllib`, for one; `httpx`, `requests` and `curl` pass. Check again when the
consumer changes its HTTP client.

To rotate the service token, create a second one, add it to the policy, move
the consumer to it, then remove the first from the policy and delete it. A
service token expires (after a year by default) and every request is then
refused with 401, so record the date. To rotate the tunnel's token, refresh
it in the account, replace the file and restart the unit. API credentials
rotate as [Secrets](#secrets) describes: deploy the old and the new one side
by side, move the consumer, remove the old one.

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
checks; it never performs DNS resolution in Pydantic validators. Every URL is
checked lexically immediately before transport use, and its hostname is
resolved: a name with any non-public address is refused, and so is a name
that does not resolve when a proxy or a remote browser would resolve it
instead (a direct fetch of such a name fails by itself).

Within one run, a hostname that has just resolved to public addresses only is
not resolved again for 30 seconds. The run's targets, redirects, pagination
URLs, browser subrequests and its proxy or CDP endpoint share that verdict,
and requests that arrive while a name is being resolved wait for that one
answer, whatever it is. No other answer is remembered: a name that does not
resolve, or that resolves to a non-public address, is resolved again by the
next request, and it loses any verdict the run still held for it. A URL longer
than 2,048 characters, or not written in ASCII as a browser writes its
requests, is resolved every time. Verdicts are not shared between runs.

Direct basic HTTP requests still resolve for every connection and connect to
the validated address with the original Host header and TLS SNI. Browser
navigation, redirects, subresources, service workers, CDP, and proxy
connections may resolve independently, so only a connected-IP
host/orchestrator boundary closes their validation-to-connection races. The
remembered verdict does not change that: the browser, proxy or remote endpoint
resolves the name again by itself after the guard, whether the guard's own
answer is a moment or 30 seconds old.

One destination of that race is out of the host policy's sight: the
container's own addresses. A name that answers a public address to the guard
and `127.0.0.1` to the browser sends the browser to the application's own API
on the container's loopback, and that connection never leaves the container
(TD-90; seen on the production host with a `dynamic` fetch). What closes it is
on the receiving side: `SCRAPEYARD_ALLOWED_HOSTS` names the hosts a request
may give in its `Host` header, and any other gets `400` before the request
reaches anything. A browser sent there by a rebinding name gives that name,
so it reads nothing; the smoke lane proves this for each browser fetcher.
A rebinding *proxy* name would carry its target's `Host` instead, which can
be a name on the list, so a request that comes from inside the container
(over loopback, or from the container's own address) must name loopback:
`127.0.0.1`, `::1` or `localhost`. Only a caller that may choose a proxy
could try that: one with `transport-admin`, or any caller in trusted mode.
A port the application is published on, at an address its container can
reach (`host.docker.internal`, or the local file's `0.0.0.0`), lets such a
proxy come back through the host instead, from an address that is not the
container's; while a public name is on the list, publish 8420 nowhere the
container can reach, as the production Compose file does.
List what callers really send, without ports:

- `127.0.0.1`, required whenever the list is set: the container's health
  check and the backup ask the application there;
- the application's fixed bridge address `172.30.0.250`, if anything on the
  host calls it there;
- the public name a tunnel or reverse proxy forwards (`cloudflared` passes the
  client's `Host` on unless its `httpHostHeader` is set);
- the Compose service name `scrapeyard`, if another container calls it, and
  `host.docker.internal`, if containers reach it through the host gateway.

Empty, the default, accepts any `Host`, as before; in untrusted mode the
application then says so at its start. A name is compared without its port,
case or trailing dot, so a rebinding name is refused on any port. After
setting it, send one request along each caller's path, the tunnel's
included: a missing name leaves the container healthy, since its health
check asks on loopback, and refusing every request from that caller. Each
refusal counts in `scrapeyard_host_refusals_total`, and the log has a line
for the first in any ten seconds.

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
  `203.0.113.0/24`, IPv4-mapped IPv6, and NAT64 forms of denied IPv4 ranges,
  and the deprecated IPv4-compatible (`::/96`), IPv4-translated
  (`::ffff:0:0:0/96`) and site-local (`fec0::/10`) IPv6 ranges.

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
useful scrape targets, including all of `192.0.0.0/24`. A change to the table
reaches the host firewall only when the policy is installed again.

Policy replacement uses alternating staging chains. The installer fully builds
the inactive chain, inserts and confirms its `DOCKER-USER` jump while the old
jump is still present, and removes the old jump only afterward. An error trap
reinserts the prior jump before withdrawing the replacement if any later IPv4
or IPv6 mutation fails. Address families, tools, Docker chain prerequisites,
and rendered rules are validated before the active policy is changed.

`DOCKER-USER` sees forwarded and same-bridge traffic only. A connection from
the application to one of the host's own addresses (the bridge gateway, the
host's network addresses) is input to the host and passes none of it. The
installer therefore also adds a chain to `INPUT`, for traffic arriving on the
deployment bridge. It refuses everything from the application's address
except replies to connections the host itself opened, so a reverse proxy or a
health check on the host still reaches the application, and the application
reaches nothing the host listens on. Before 2026-10-10 that input was not
filtered: with the policy installed, the application's container connected to
a listener on the bridge gateway (TD-78).

Two things follow from that chain having no exceptions:

- Docker's DNS servers must not be one of the host's own non-loopback
  addresses (a `dnsmasq` on the bridge, for example). Docker dials such a
  server from the container's namespace, and the application could then
  resolve nothing. The usual arrangement, where the host's resolver is on
  loopback and Docker forwards to the upstream servers, is unaffected.
- An operator proxy cannot run on the Docker host itself.
  `SCRAPEYARD_EGRESS_ALLOW_CIDRS` opens only the forwarded policy.

Nothing checks that the `INPUT` jump stays in place between deployments: the
application's startup check tests forwarded traffic only. The jump is lost,
until the policy is installed again, by `iptables -F`, `nft flush ruleset`
(and so `systemctl restart nftables`, which also empties `DOCKER-USER`),
`iptables-restore` without `-n` (as `netfilter-persistent` runs it), and
`ufw` when `MANAGE_BUILTINS=yes`. A default `ufw` leaves it alone. Do not run
a second firewall manager over `INPUT` on this host without checking the jump
afterwards.

The policy depends on the kernel's bridge netfilter hook
(`net.bridge.bridge-nf-call-iptables`, and `bridge-nf-call-ip6tables` for an
IPv6 source). Redis, the probe and any other container on the deployment
bridge are reached at layer 2, and those frames pass `DOCKER-USER` only while
the hook is on. Docker Engine turned it on for every host until 27.4; newer
engines load `br_netfilter` only for a network with inter-container
communication disabled. The installer therefore loads the module and sets the
hook before it changes any chain, and stops if it cannot. The setting is
host-wide and does not survive a reboot, so the policy must be installed again
after one; the [boot service](#boot-service-and-promotion) does that. `remove`
leaves the hook as it is. If startup fails with
"controlled non-public challenge was reachable", check this setting first.

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

### Without an operator proxy

Untrusted mode is for a service that takes scrape definitions from callers
its operator does not control. When the only submitter is the operator's own
service, sending configurations from its own repository, set
`SCRAPEYARD_UNTRUSTED_SUBMISSIONS=false` and leave `SCRAPEYARD_PROXY_URL`
unset. Scrapeyard then fetches directly, from the host's address, and
`security/deploy-secure-compose.sh` no longer asks for a proxy.

What stays in force:

- The host egress policy and the startup check of it. The application still
  refuses to start unless the probe's challenge is refused, so other
  containers, private networks, link-local and metadata addresses stay
  unreachable from its container whatever a page or a redirect asks for. The
  wrapper refuses a deployment that sets the probe's address or ports, which
  the Compose file fixes, or a probe timeout under one second: a challenge
  that times out counts as refused.
- The application's URL guard, Chromium's sandbox and the container's
  confinement.

The container's own loopback never leaves its network namespace, so no host
policy covers it; only the URL guard refuses it. Nothing but the application
listens there.

What is given up:

- A second filter outside the host. Destination filtering rests on the host
  policy and the URL guard alone.
- The `transport-admin` gate. Any credential with `submit` can name a proxy
  or a remote browser (`browser.cdp_url`) for its own targets; both must
  still be public addresses.
- The host's address is the one target sites see. A site that blocks it can
  be given a proxy by itself, with `proxy:` on that job or target; nothing
  about the deployment changes.

A host set up this way cannot roll back to a release built from a commit
before this mode existed: that release's wrapper asks for the proxy and
refuses before it stops anything.

Two browser races are accepted in the application and left to this policy,
which denies their destinations whatever the application decided:

- The route guard answers asynchronously. If a page removes a cross-site
  iframe, or navigates it away, while one of that frame's requests is waiting
  for the guard, the browser can let the request go before the guard aborts
  it. Requests from same-site frames, popups and top-level navigations are
  aborted as usual. Measured on 2026-10-09 with Chromium 136 and 145; the
  run's hostname verdicts do not change it, because a non-public host never
  gets one.
- A browser that is closed from outside, or dies, while its page is online
  can send a request it had started by itself, such as `/favicon.ico`,
  without handing it to the guard. Scrapeyard takes a context offline before
  it closes a page or a browser, so its own shutdown paths do not do this.

Kubernetes deployments should express the same allow-before-deny policy with a
CNI that supports egress CIDR rules; default Kubernetes `NetworkPolicy` cannot
select arbitrary public destinations while excluding every rebinding target.

## Retained release artifacts and rollback

Prepare a release on a build/qualification host with Docker, Python 3.10+, and
at least 15 GiB free disk. Use an isolated host or ensure the qualification
lane's fixed subnets and ports are unused. This builds the selected **committed**
revision, runs the existing security scans and complete quick qualification,
and publishes the directory only after both pass. The vulnerability gate
covers all three images the release ships: the application, Redis and the
egress probe. A finding in Redis or the probe that only an upstream rebuild
fixes, and that does not apply here, can be accepted in
`security/trivy-dependency-exceptions.yaml` with its reason and an expiry;
after the expiry the gate fails again until the digest is refreshed. Trivy
cannot see a source-built `redis-server`, so follow Redis's own advisories
too. `create` runs the scanner of the revision it builds, and before TD-99
that scanner passed over Redis and the probe; `verify --scanned` refuses a
release without a report naming each of its images, so use it on any
release you mean to ship (a rollback target may predate it):

```bash
python3 scripts/release_artifact.py create ~/scrapeyard-releases/candidate --revision HEAD
python3 scripts/release_artifact.py verify --scanned ~/scrapeyard-releases/candidate
```

A GitHub runner is such a host. The `Release artifact and deployment
rehearsal` workflow prepares the release there, deploys it and rolls it back
(see [TESTING.md](TESTING.md#release-and-deployment-rehearsal)), and uploads
it:

```bash
gh workflow run release-rehearsal.yml --ref main \
  -f revision=<commit> -f previous_revision=<currently deployed commit>
gh run download <run-id> -n scrapeyard-release-<commit>
sha256sum -c scrapeyard-release-<commit>.tar.zst.sha256
sudo tar --zstd -xpf scrapeyard-release-<commit>.tar.zst --no-same-owner -C /srv/scrapeyard-releases
python3 /srv/scrapeyard-releases/scrapeyard-release-<commit>/source/scripts/release_artifact.py \
  verify --scanned /srv/scrapeyard-releases/scrapeyard-release-<commit>
```

Compare the checksum with the one in the run's summary. `--no-same-owner`
makes root the owner of every extracted file, which the
[boot service](#boot-service-and-promotion) requires. The artifact expires
after 30 days, so it is a way to move a release, not a place to keep one.

Build releases in a directory of the build account's own, as the workflow
does. On the deployment host `/srv/scrapeyard-releases` belongs to root, and
releases reach it only by being extracted there as root.

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

Before promotion, pause new ingress/dispatch, take a
[backup](#persistence-and-backups) (`sudo systemctl start
scrapeyard-backup.service`), and
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
/srv/scrapeyard-releases/candidate/source/security/deploy-secure-compose.sh \
  --release /srv/scrapeyard-releases/candidate
```

That is the wrapper the release retains, so no checkout is needed on the host.
On a host with the [boot service](#boot-service-and-promotion),
`scrapeyard-release promote` runs this command with the settings from
`/etc/scrapeyard/deploy.env` and records the release for the next boot.

The wrapper verifies and loads the bundle, checks all images before stopping
the app, starts dependencies, installs the retained egress/AppArmor policies,
then starts the app with `--no-build --pull never`. The source and `images.json`
paths in the bundle are the only Compose files selected in release mode. A
failure before stopping leaves the current app running. Any failure or
interruption after that leaves it stopped: a policy that cannot be installed,
and also a start that fails, whose container would otherwise go on restarting
and could come up later. The wrapper holds a lock under `/run/scrapeyard`, so
a second deployment waits for the first. A container that Docker cannot stop
(one that dies before it is up is restarted without end, and `docker stop`
fails on it) is removed, and the next start creates it again. Its last output
is kept in `/run/scrapeyard/removed-container.log`, which only root can read,
and not in the wrapper's own output: a start that fails on a setting can print
part of that setting. If the container cannot be removed either, the wrapper
says so in place of "the application is left stopped"; look at
`docker ps --all`. Do not resume ingress until
authenticated `/health/ready`,
a known `/results/{id}` read, effective host/security policy, and exactly one
application container/process have been verified. The container's health
is the application's readiness when the deployment has a `health-detail`
credential, so the wrapper's start has already waited for it; a deployment
without one is checked for liveness only. That also changes what a start
does with an application that runs and is not ready, because a dependency or
a background task is failing or free disk is under its floor: the start
fails after about two minutes and the wrapper leaves the application
stopped, where it used to leave it serving. The boot service then retries
with its growing pauses, and nothing the application does for itself, its
retention cleanup included, happens until a start succeeds. Retain redacted
evidence of those checks after each transition.

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

## Boot service and promotion

A reboot loses everything the deploy wrapper installs on the host: the
`DOCKER-USER` policy chains, the bridge netfilter setting and the loaded
AppArmor profile. Scrapeyard refuses to serve without them, so after a reboot
it stays down until the wrapper runs again.
`security/systemd/scrapeyard.service` runs it at boot and whenever Docker is
started, for the release that was last promoted.

The unit runs `/usr/local/sbin/scrapeyard-release`, an installed copy of
`security/manage-release.sh`. It is installed once and is not read from the
promoted release, so the unit keeps working after a rollback to a release that
predates the script. The two expect this layout:

- `/srv/scrapeyard-releases/<release>` holds each retained release, and
  `/srv/scrapeyard-releases/current` is a link to the promoted one. Root runs
  a release at every boot, so root must own every file and directory of it,
  and every directory above it up to `/`, and nobody else may be able to
  write to any of them. A release or a link in a directory that fails this is
  refused. Create the directory as root and extract archives into it as root
  with `tar --no-same-owner`; do not build releases there with another
  account.
- `/etc/scrapeyard/deploy.env` holds the deployment settings. It is owned by
  root with mode 600. Each line is `NAME='value'`, with the value in single
  quotes (the JSON settings contain double quotes). The name is
  `COMPOSE_PROJECT_NAME` or starts with `SCRAPEYARD_`; `COMPOSE_ENV_FILES` and
  other Compose variables are refused, so that no second settings file is
  read. Empty lines and lines starting with `#` are skipped. A value cannot
  contain a single quote, and nothing may follow the closing quote, not even
  a space. It needs `COMPOSE_PROJECT_NAME`, `SCRAPEYARD_API_CREDENTIALS`,
  `SCRAPEYARD_ENCRYPTION_KEYS` and `SCRAPEYARD_ENCRYPTION_ACTIVE_KEY_ID`;
  either `SCRAPEYARD_PROXY_URL` or, for a deployment
  [without an operator proxy](#without-an-operator-proxy),
  `SCRAPEYARD_UNTRUSTED_SUBMISSIONS='false'`; and any other setting the
  Compose file or the policy installer reads (`SCRAPEYARD_EGRESS_ALLOW_CIDRS`,
  for example).

`scrapeyard-release` reads that file itself; it is not a systemd
`EnvironmentFile=`, and systemd never sees it. A line it cannot read stops the
command with the line's number and nothing else, so a mistyped line does not
put a secret in the journal.

Install the first release:

```bash
sudo install -d -m 755 -o root -g root /srv/scrapeyard-releases   # before extracting into it
RELEASE=/srv/scrapeyard-releases/scrapeyard-release-<commit>
sudo install -d -m 700 /etc/scrapeyard
sudo install -m 600 /dev/null /etc/scrapeyard/deploy.env   # then fill it in
sudo ln -s "$RELEASE" /srv/scrapeyard-releases/current
sudo install -m 755 "$RELEASE/source/security/manage-release.sh" /usr/local/sbin/scrapeyard-release
sudo install -m 644 "$RELEASE/source/security/systemd/scrapeyard.service" /etc/systemd/system/
sudo install -m 644 "$RELEASE/source/security/systemd/scrapeyard-policy-watch.service" \
  "$RELEASE/source/security/systemd/scrapeyard-policy-watch.timer" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now scrapeyard.service
sudo systemctl enable --now scrapeyard-policy-watch.timer
```

Install the [host check](MONITORING.md#the-host-check) beside them, and the
[backup](#persistence-and-backups) once its key pair exists.

Repeat the `install` lines and `systemctl daemon-reload` from a later release
to take a newer script or unit. The [policy watch](#the-policy-watch) needs
the script of the release that introduced it: with an older installed script
its unit fails every minute with the script's usage.

Promote a later release, or roll back to a retained one, with one command:

```bash
sudo scrapeyard-release promote /srv/scrapeyard-releases/scrapeyard-release-<commit>
```

`promote` checks the release and the settings file, then runs the deployment
as the systemd unit `scrapeyard-promote.service`: that release's own wrapper,
followed by moving the `current` link. Its output is printed when it ends;
`journalctl -fu scrapeyard-promote` follows it meanwhile. How it can end:

- **Promoted.** The release is running and the boot service starts it from
  now on.
- **Refused before anything was stopped** (the release fails verification, a
  file in it is not root's, the settings file is malformed, an image is
  missing). The earlier release keeps running and the link stays on it.
- **Failed after the application was stopped** (the policy could not be
  installed, the application did not start or did not attest the policy). The
  wrapper leaves the application stopped, not restarting, and the link stays
  on the earlier release. Promote that release again to return to it. If the
  wrapper ends with "could not be stopped or removed" instead, Docker would
  not let go of the container: it may still be restarting, and the next
  deployment is refused until it has been cleared.
- **Stopped** with `systemctl stop scrapeyard-promote.service`. It is reported
  as a failure and the link is not moved. Depending on how far it had got, the
  earlier release is still running or the application is stopped. `promote`
  waits until the interrupted deployment has finished stopping and then says
  which. Ctrl-C and a closed terminal do not stop a promotion: it finishes in
  its unit, link included.
- **Deployed, but the link could not be moved** (a full or read-only
  filesystem). The application is stopped again, so that nothing runs which
  the next boot would replace. A directory that cannot be written to is
  normally caught before anything is stopped.

When a deployment that had started does not complete, `promote` ends by
saying what the boot service starts and what is running: the earlier release,
nothing, or (if the deployment was killed after it started the application)
the release that was not promoted. In the last two cases promote a retained
release to settle it. A release refused by the checks before the deployment
changes nothing, and `promote` says only why it was refused.

Only one start or promotion runs at a time. Each takes a lock before it reads
the `current` link and keeps it until the link has been moved, so a start of
the boot service that had to wait for a promotion starts the release that was
promoted. A second `promote` while one is under way is refused.

What the unit does:

- **Boot, and a start of Docker.** After Docker starts it verifies and
  loads the promoted release, starts Redis and the probe, installs the egress
  policy and the AppArmor profile, and starts the application. If Docker's
  first start fails and Docker recovers by itself, the unit still starts. One
  case is not covered: if `docker.service` ends up failed while this unit is
  still active, starting Docker again does not run it again; use
  `systemctl restart scrapeyard.service`.
- **Shutdown, `systemctl stop scrapeyard.service`, and `systemctl stop
  docker`.** While the unit is active, it stops the application container,
  with the container's own 40-second grace period, before Docker stops. Docker
  then does not start the application by itself at the next boot, ahead of
  the policy. The stop needs `deploy.env` to be in place with a valid
  `COMPOSE_PROJECT_NAME` line, and ignores the rest of the file. If it cannot
  read that, or Docker does not answer, it stops nothing and the unit is left
  in the failed state; `systemctl stop` itself still returns success, so
  check `systemctl status scrapeyard.service`. When the unit is not
  active (after a power loss, or while it is between retries), nothing stops
  the container first. Docker then tries to start it at the next boot, and
  the application cannot start (the AppArmor profile is gone) or refuses to
  serve (the challenge is reachable) until the unit has run.
- **A restart of Docker**, by `systemctl restart docker` or by systemd after
  the daemon crashed. The unit is restarted with it and deploys the promoted
  release again, which restarts the application.
- **A recreated deployment network** (`docker compose down`, then `systemctl
  restart scrapeyard.service`). The network gets a new bridge. The installer
  removes the policy jump left for the bridge that no longer exists and
  installs the policy on the new one.
- **A start that fails.** The application is left stopped. The unit tries
  again after a minute and then after longer pauses, up to an hour, for as
  long as it takes: a setting repaired in `deploy.env` is picked up by the
  next attempt. It never refuses a start for having been started too often.
  To retry at once, use `systemctl restart scrapeyard.service`; on systemd 255
  a plain `start` waits for the current pause to end.

The lock between starts and promotions, the settings parser and the ownership
checks are in the installed script, so they apply to every release. Leaving
the application stopped after a failed start, the wrapper's own lock and the
handling of a recreated network are in each release's wrapper and installer;
a release built from a commit before `2c0f4be` does not have them.

`systemctl restart scrapeyard.service` deploys the promoted release again. It
is not the way to promote another one: it stops the application before the
wrapper has verified anything.

### The policy watch

The application checks the host policy once, at its start. Between
deployments the policy can be taken away without the application noticing.
A restore of rules saved before the policy was installed (`iptables-restore`,
which `netfilter-persistent` runs) removes the jumps in `DOCKER-USER` and
`INPUT`, and after it the application reaches the host's own addresses and
whatever the forwarded policy refused. A flush of the host's rules
(`iptables -F`) removes them too, and Docker's own jump from `FORWARD` with
them: what the application then reaches depends on the `FORWARD` chain's
policy, nothing forwarded under `DROP` and everything under `ACCEPT`. `ufw`
does neither: its reload, and disabling and enabling it, left both jumps in
place when tried on Ubuntu 24.04.

`scrapeyard-policy-watch.timer` runs `scrapeyard-release watch` every minute.
It asks the promoted release's installer whether the policy is in place
(`install-docker-egress-policy.sh check`, which changes nothing): the bridge
netfilter setting, Docker's own jump from `FORWARD` to `DOCKER-USER`, the
`DOCKER-USER` jump for the deployment's bridge with a chain behind it that
still refuses the application's address, and the `INPUT` jump with its
refusal. What it then does:

- **In place:** nothing, and it prints nothing. The unit keeps its own start
  and finish lines out of the journal.
- **Missing:** it installs the policy again with the same installer, checks
  once more, and leaves the application running. The journal gets a warning
  that names what was missing, and `/var/lib/scrapeyard/policy-restored.log`
  gets a line with the time and the same text. Until an alert reads that
  file (TD-20), it is the record that the policy was gone, for a minute or
  so if the watch was running.
- **Missing, and it cannot be put back:** it stops the application, writes a
  `NOT PUT BACK` line to the same file, restarts `scrapeyard.service` and
  fails. The boot service installs the policy before it starts the
  application, and the application checks the forwarded policy before it
  serves, so for anything that check covers the application stays down until
  the policy is in place, with the boot service retrying. Docker's jump from
  `FORWARD` is one such case, and an install cannot repair it: restart
  Docker, which restarts the boot service with it.
- **Still missing after that restart, with the application running:** the
  application's own check does not cover everything the watch looks at. It
  does not cover the `INPUT` jump, nor an IPv6 policy. If something keeps
  removing one of those, the application comes back from the restart and
  the next run finds the same thing. The watch does not stop it a second
  time for the same finding, which would take the service down every minute
  and change nothing. It fails every minute with the finding, and **the
  application runs without what the finding names** until the cause is
  removed. The finding is remembered under `/run/scrapeyard` without the
  `-A` or `-B` at the end of a chain's name, which changes with every
  install, and is forgotten when the policy is found in place or put back,
  and at a reboot.
- **Nothing to judge:** while a start or a promotion holds the release lock,
  while the application is not running, or when the promoted release is
  older than the `check` action, it does nothing.

It reads that the jumps and the refusals exist. It does not judge their
order and sends no packet: a rule that another tool puts ahead of a jump, or
ahead of the refusals inside one of the policy's chains, is not something it
sees.

## Container privilege and filesystem boundary

The image is built for Linux `amd64` and runs directly as UID/GID `10001`.
Chromium uses unprivileged user namespaces, so the image contains no privileged
setuid helper and startup performs no root repair. Scrapeyard starts Chromium
with its sandbox on every browser fetch, with `stealth` and without, and never
passes `--no-sandbox`. Outside this container, on a host that denies
unprivileged user namespaces (Ubuntu 23.10 and later do, to programs without
an AppArmor profile), a browser fetch fails with "No usable sandbox" until the
host allows them. Before starting Compose,
load the path-independent host profile:

```bash
sudo security/install-chromium-apparmor-profile.sh install
```

The profile is Moby's normal container boundary plus the `userns`
permission, and like Moby's it denies `AF_ALG` and `AF_VSOCK` sockets. It
also denies the `net_admin` and `net_raw` capabilities and packet sockets,
for the reason given below.
Compose also applies a seccomp allowlist, enables no-new-privileges, drops
every capability, and keeps `SYS_CHROOT` alone in the container's bounding
set. The application process has no permitted or effective capabilities.

That one capability is there for the seccomp filter: Docker keeps the
filter's `chroot` rule only for a container whose bounding set holds it, and
the browsers' sandboxes call `chroot` inside their user namespace. The
bounding set does not limit that namespace. The kernel gives a process every
capability inside a user namespace it creates, so what a process can do
there is bounded by the seccomp filter, whose capability-gated rules were
decided when the container started, and by the AppArmor profile.

With a network namespace of its own beside the user namespace, such a
process holds `CAP_NET_ADMIN` and `CAP_NET_RAW` over a network stack: packet
and raw sockets, netfilter tables, links and traffic control, which is kernel
code where local privilege escalations have been found before. A renderer
cannot get there, because the browser's own sandbox refuses it the calls;
code that has taken over the browser process, the driver or the application
could. AppArmor judges a capability by the profile wherever it is used,
inside a user namespace too, so the profile denies those two capabilities
and the packet family. Neither browser's sandbox uses them: it needs the
namespaces, `chroot` and to read the route table, which stay allowed. The
container smoke lane tries each of these from a namespace of its own
(`tests/smoke/verify_confinement.py`).

All of this is AppArmor's doing. Where Docker applies no AppArmor profile (a
host without AppArmor, or a daemon that cannot use it, such as one running
inside a container manager), it starts the container unconfined, under the
profile's name, and none of these denials exists. The deploy wrapper guards
against both. Before it stops anything it refuses a host whose Docker
reports no AppArmor. After the start it reads what the kernel reports for
the container's first process, and unless that is `scrapeyard-chromium
(enforce)` the deployment fails and the application is left stopped. A plain
`docker compose up` checks neither. The profile still allows every other
capability inside such a namespace (TD-88).

The allowlist (`security/seccomp/chromium.json`) is the default profile of
Docker 29.9.0 plus one rule: `clone` and `unshare` are allowed whatever
their arguments, because the browsers' sandboxes create user namespaces and
the default allows that only with `CAP_SYS_ADMIN`.
[`security/seccomp/README.md`](../security/seccomp/README.md) records where
the default comes from, the difference, and how to move to a newer one. Both
files are generated or copied; edit neither.

Two system calls decide whether anything starts, and a test holds each:
- runc 1.3.3 (the release that fixed CVE-2025-31133, CVE-2025-52565 and
  CVE-2025-52881) opens `/proc/thread-self/fd` with `openat2` after it has
  loaded the filter. A profile that refuses the call starts no container and
  no `docker exec` ("unsafe procfs detected"), which the profile used until
  2026-10 did on the first production host.
- `clone3` answers `ENOSYS`, as in the default. glibc falls back to `clone`
  on `ENOSYS` only; with the default `EPERM` no thread would start. runc
  answers `ENOSYS` by itself only for a system call numbered above the
  highest one a profile names.

Do not replace either policy with `unconfined`. `/data`, bounded `/tmp`, `/run`,
home cache/config and Camoufox state tmpfs, and `/dev/shm` are the only writable
areas.

No browser process dumps core. Chromium is told that a renderer died only when
the renderer's process exits, and the kernel holds a crashed process until its
core dump is written. A host whose `kernel.core_pattern` pipes dumps to a
program (`systemd-coredump`, `apport`) receives them from containers as well,
and ignores the container's core ulimit when it does. On a GitHub runner that
left a crashed page unanswered for 219 seconds, and a renderer that ran out of
memory left a 548 MB compressed dump of it under the host's
`/var/lib/systemd/coredump` (2026-10-10). Before it starts a browser,
Scrapeyard therefore gives itself a soft core limit of 1, the one value the
kernel honours for a piped dump, and an empty `/proc/self/coredump_filter`.
Every browser process inherits both. The application process is covered too,
so it leaves no core dump either.

Fresh named volumes inherit UID 10001 from the image. Migrate an older or host
bind-mounted data directory while the application is stopped:

```bash
docker run --rm --user 0 -v scrapeyard_scrapeyard-data:/data \
  redis:7.4.11-alpine@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499 \
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
  permitted `project` filter. `submit` also lets a caller cancel an exact
  `run_id` of an ad-hoc job in its projects; every other cancellation and all
  deletion need `delete`.
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
- Define each secret a job may name as its own line in the settings file,
  for example `SCRAPEYARD_SECRET_PROXY_URL='http://user:password@proxy.example:8080'`
  (at least eight characters). The Compose file cannot list names it does not
  know, so the deploy wrapper finds the `SCRAPEYARD_SECRET_*` names in its
  environment that the allowlist names for some project, writes them, and
  only the names, to `/run/scrapeyard/secret-names.compose.yml`, and adds that
  file to the deployment; Compose takes each value from the wrapper's
  environment. A secret no project may name does not reach the container. A
  change to a secret takes effect at the next start of the boot service.
  A job that names a secret the application does not hold is refused with
  `422` and the name.
- That works through `scrapeyard-release`, which reads the settings file into
  the wrapper's environment. Run by hand, the wrapper passes the secrets and
  the allowlist in its own environment only, so name both in `sudo
  --preserve-env=...`. A manual `docker compose up`, or `COMPOSE_ENV_FILES`,
  passes the allowlist and none of the secrets. So does a rollback to a
  release from before this (2026-10-14): jobs that name a secret are then
  refused until a newer release is promoted again.
- Allowing a name for a project gives its value to every credential that may
  submit to that project. A reference is resolved in any string of the job,
  so a submitter can put it in a target or webhook URL and send it anywhere;
  in untrusted mode only `proxy` and CDP fields need `transport-admin`. Allow a
  secret only for a project whose every submitter may see it.

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

A deployment keeps its state in three places:

- the volume `<project>_scrapeyard-data`, mounted at `/data`: the three SQLite
  databases under `/data/db`, the result artifacts under `/data/results`, the
  adaptive selector state under `/data/adaptive`, and logs;
- the volume `<project>_redis-data`: the queue, each host's page count for
  the day and each host's cooldown after a denial;
- `/etc/scrapeyard/deploy.env`: the API credentials and the encryption
  keyring.

A copy of the first two taken while the application runs is not a backup:
the databases, the result files and Redis would each be from a different
moment. `security/backup.sh` takes one coordinated copy with the application
stopped, and proves a copy by restoring it. It is installed as
`/usr/local/sbin/scrapeyard-backup`, a copy that is not read from the promoted
release, for the reason [`scrapeyard-release`](#boot-service-and-promotion)
is one. The backup helper and the SQL a copy is checked against do come from
the promoted release (`scripts/qualification_backup.py`, described in
[TESTING.md](TESTING.md#quiesced-backup-and-fresh-restore)).

### What a backup run does

`scrapeyard-backup.timer` starts `scrapeyard-backup.service` once a day at
04:40 UTC, which runs `scrapeyard-backup run`:

1. **Refuses what it can before anything is stopped**: no promoted release; a
   recipients file or upload hook that is missing a requirement below, or a
   recipients file that `age` itself does not accept; a settings file with a
   line the unit's start could not read (below); a promotion under way; a
   boot service that is not active; or too little room under
   `/var/backups/scrapeyard`. The data is measured first, read-only, while
   the application runs, and a run needs twice that and 1 GiB beside: the
   copy of the files and the set made from it are on the disk together.
2. **Waits until the application is idle**, for at most an hour, asking every
   30 seconds: readiness is `ok`, nothing is running and nothing is queued.
   The question is put from inside the application's container
   (`docker exec`), with the `health-detail` credential read from the
   container's own environment, so no credential is read on the host or
   appears in a process list. The journal says why it waits, once for each
   new reason. If the application is never idle, the run fails with nothing
   stopped and names the last answer. An application whose readiness answers
   503 is not backed up, idle or not: the run ends after the hour with
   "unhealthy" as that answer.
3. **Takes a witness** through the same route, in the same exchange that found
   the application idle: the list of jobs the configured `read` credentials
   can list, and for the most recent `complete` job and the most recent
   `failed` job the SHA-256 of what `GET /jobs/{id}` and `GET /results/{id}`
   return. A job with no result is recorded as such (404); any other answer
   to a read, a rate limit for one, is a refusal, and the question is asked
   again at the next poll. The reads are made with the consumer's own
   credential and count towards its rate limit, so the job list is asked for
   in the largest pages the API allows. The witness holds no credential.
4. **Stops the application** with `systemctl stop scrapeyard.service`, while
   holding the lock that every start and promotion takes
   (`/run/scrapeyard/release.lock`), and confirms that no application
   container is running; a Docker that does not answer that question fails
   the run there. The lock is taken together with the last idle answer, so
   no start or promotion can begin between the two. It does not keep a
   submission out: see the archive's `accepted_after_witness` below.
5. **Copies.** Redis is told to `SAVE` and its `dump.rdb` is copied out. The
   promoted release's helper then runs in a container of the application's
   image, as the data's owner, with no network and the data volume mounted
   read-only, and writes a backup set: the three databases through SQLite's
   backup API, the result and adaptive files, and a manifest with every
   file's size and SHA-256, validated before the helper returns. SQLite
   cannot open a database in WAL mode from a read-only directory, so the
   helper reads a copy of the volume's files made in the same container, not
   the volume itself.
6. **Starts the application** with `systemctl start scrapeyard.service` and
   asks it for readiness. That is the only way a backup starts it, because it
   is the path that installs the egress policy and has the application check
   it.
7. **Packs and encrypts**, with the application already back: one `tar`
   stream compressed with zstd and encrypted with `age` to every recipient in
   `/etc/scrapeyard/backup-recipients`. The stream goes from `tar` straight
   into `age`. The unencrypted copy is removed as soon as it is packed.
8. **Keeps the newest seven archives** on the host, hands the new one to
   the upload hook if one is installed, and writes
   `/var/lib/scrapeyard/backup-status.json`.

The application is down from step 4 to step 6: the stop, the copy and the
start. With a few hundred kilobytes of data that was 17 to 21 seconds in
seven runs on GitHub runners (2026-10-13), nearly all of it the application's
own stop and start; with 2 GiB of data it was 30.5 and 39.8 seconds in two
runs, and the restore test of that archive took 40.5 seconds. It has not
been measured on a production host, whose cores are slower. A request that arrives
in that time is refused, so keep the consumer's schedule clear of the
minutes after 04:40 UTC. Work that was queued when the application stopped
is in both the databases and Redis's dump, from the same moment; a backup
waits for an idle application, so normally there is none.

A backup is an unattended restart. It stops the application and starts it
through its unit, so it applies whatever `/etc/scrapeyard/deploy.env` holds
at that moment, as `systemctl restart scrapeyard.service` would: an edit not
yet applied takes effect at 04:40. Before it stops anything, a run reads the
file as the unit's start reads it, and refuses a line that start could not
read, naming the line by its number. A setting that reads well and that the
deploy wrapper or the application then refuses (a malformed keyring, a proxy
name that does not resolve) is still found only at the start: the
application stays down and the unit retries it, as after any failed start.
Do not leave a half-made edit in the file.

How a run can end:

- **Completed.** The status file is rewritten. Only this outcome rewrites it,
  so its `completed_at` is the age of the last good backup.
- **Refused, or never idle (or unhealthy).** Nothing was stopped. The unit is
  failed and the journal says why (`journalctl -u scrapeyard-backup`).
- **Failed after the application was stopped** (the copy failed, the run was
  stopped or timed out). The run starts the application again through
  `scrapeyard.service` before it ends, and removes the unencrypted copy.
- **Killed outright**, so that it could do neither. The unit runs
  `scrapeyard-backup resume` after every run, however it ended: it starts
  `scrapeyard.service` if a run left the application stopped, and removes
  what the run left in `/var/backups/scrapeyard`. A reboot in the middle of a
  backup starts the application as any boot does.
- **Archive written, but the application did not come back ready, or the
  upload hook failed.** The archive is kept, the run is failed, and the
  status file is left as it was.

The run's limits are settings of the unit, in a drop-in
(`systemctl edit scrapeyard-backup.service`, then
`Environment=SCRAPEYARD_BACKUP_KEEP=14`): `SCRAPEYARD_BACKUP_KEEP` (archives
kept on the host, 7), `SCRAPEYARD_BACKUP_IDLE_TIMEOUT_SECONDS` (3600),
`SCRAPEYARD_BACKUP_IDLE_POLL_SECONDS` (30), `SCRAPEYARD_BACKUP_MIN_FREE_MB`
(1024, the room wanted beside what the copy itself takes),
`SCRAPEYARD_BACKUP_SNAPSHOT_TIMEOUT_SECONDS` (900) and
`SCRAPEYARD_BACKUP_UPLOAD_TIMEOUT_SECONDS` (1800). The restore test and
restore read `SCRAPEYARD_BACKUP_MIN_FREE_MB` too, and
`SCRAPEYARD_BACKUP_READY_TIMEOUT_SECONDS` (300): how long the restored copy
may take to be ready. Give those on the command line, as in
`sudo env SCRAPEYARD_BACKUP_READY_TIMEOUT_SECONDS=600 scrapeyard-backup
restore-test ...`. None is a secret. Change
the time of day with a drop-in for the timer (an empty `OnCalendar=` line,
then the new one), and keep it well away from the hour at which the host
reboots for updates ([Host baseline](#host-baseline)).

### What an archive holds, and what it does not

`/var/backups/scrapeyard/scrapeyard-backup-<UTC time>.tar.zst.age`, with its
SHA-256 in a `.sha256` file beside it, holds:

- `set/`: the helper's backup set (`manifest.json` and `payload/`);
- `redis/dump.rdb`;
- `witness.json`;
- `metadata.json`: the format (`scrapeyard-host-backup-v1`), the UTC time of
  the copy, the name of the release that made it and its application image,
  the Compose project, the number of keys in Redis's dump, the SHA-256 of
  the set's manifest, and `accepted_after_witness`: the jobs that are in the
  copied database and not in the witness, because they were submitted in
  the second between the last idle answer and the stop (only jobs in the
  projects the witness covers are counted). The run's journal says when
  there are any. It names 1,000 at most, and `accepted_after_witness_count`
  says how many there were; an archive that could not name them all is
  refused by the restore test, which could not tell them from a difference,
  and can still be restored. Archives written before 2026-10-14 have neither
  field: the restore test refuses them saying so, and a restore takes them.
  The format names what an archive holds: a change that an older script
  could not read gets a new one.

It deliberately does not hold:

- **`/etc/scrapeyard/deploy.env`.** The stored job configurations and pending
  webhook requests in the databases are encrypted with the keyring in that
  file ([SECRET_STORAGE.md](SECRET_STORAGE.md#rotation-backup-restore-and-key-loss)),
  and an archive that carried the key would undo that. Keep a copy of the
  file somewhere that is neither the host nor the place the archives go: the
  operator's password manager, or an `age`-encrypted file on other storage.
  Renew that copy whenever the file changes (a rotated credential, a new
  key). A restore needs the key IDs the envelopes were written with. Without
  them the stored configurations cannot be read by anyone, and the consumer
  needs a new credential for the rebuilt host. So a key retired from the
  running keyring stays in the kept copy, or a copy of its own, for as long
  as any archive made while it was in use is kept, on the host or in the
  store.
- **The release.** Keep the retained release archive as
  [above](#retained-release-artifacts-and-rollback); a restore needs the
  release that made the backup.
- **`/data/logs`**, the tunnel's token and anything else on the host.

`age` encrypts to a public key and signs nothing. An archive is therefore
confidential, and it is not proof of who made it: anyone who holds the public
key and can write to the store can put an archive there. Record the SHA-256
of an archive (it is in the status file and in the run's journal) somewhere
the store's credentials cannot change it, and compare before a restore.

### Installing it

`age` comes from the distribution (`age` 1.1.1 on Ubuntu 24.04), and `zstd`
is already there for the release archives:

```bash
sudo apt-get install age
RELEASE=$(readlink -f /srv/scrapeyard-releases/current)
sudo install -m 755 "$RELEASE/source/security/backup.sh" /usr/local/sbin/scrapeyard-backup
sudo install -m 644 "$RELEASE/source/security/systemd/scrapeyard-backup.service" \
  "$RELEASE/source/security/systemd/scrapeyard-backup.timer" /etc/systemd/system/
sudo systemctl daemon-reload
```

Generate the key pair **on another machine**, never on the host:

```bash
age-keygen -o scrapeyard-backup.agekey   # prints "Public key: age1..."
```

- The file is the private half (the identity). It stays off the host: in the
  operator's password manager, and a second copy somewhere that survives the
  loss of the first. Whoever has it can read every backup.
- The public half is the line starting `age1`. It is the only half the host
  gets, so the host can write backups and cannot read them, its own included.

Give the host the public half, one recipient to a line. A second recipient,
whose private half someone else keeps, means one lost key does not lose the
backups:

```bash
echo 'age1...' | sudo tee /etc/scrapeyard/backup-recipients
sudo chmod 644 /etc/scrapeyard/backup-recipients
```

The file must be a regular file of root's that nobody else can write, in
directories of which the same is true, and `age` must accept every line; a
run refuses it otherwise, before anything is stopped. A run also refuses a
file that holds a private key (`AGE-SECRET-KEY-...`), whatever else is in
it: take the key off the host. For as long as it was there, root on the host
could read every archive made for that key. That is the exposure a restore
test has for its few minutes (below), only longer; whether to replace the
pair afterwards is the same judgement.

Take the first backup at once and read what it did, then switch the timer on:

```bash
sudo systemctl start scrapeyard-backup.service
sudo journalctl --no-pager -u scrapeyard-backup -n 20
sudo cat /var/lib/scrapeyard/backup-status.json
sudo systemctl enable --now scrapeyard-backup.timer
systemctl list-timers scrapeyard-backup.timer
```

Run a backup only through its unit. The unit is what starts the application
again after a run that is killed. Repeat the two `install` lines and
`systemctl daemon-reload` from a later release to take a newer script or
units.

The status file holds no secret and is readable by any account, for a
monitoring check:

```json
{
  "application_down_seconds": 37.2,
  "archive": "/var/backups/scrapeyard/scrapeyard-backup-20261013T044012Z.tar.zst.age",
  "bytes": 181234,
  "completed_at": "2026-10-13T04:41:02Z",
  "format": "scrapeyard-backup-status-v1",
  "release": "scrapeyard-release-<commit>",
  "sha256": "<of the archive>",
  "uploaded": true,
  "waited_for_idle_seconds": 0
}
```

### The upload hook

Until a hook is installed, every archive is on the host's own disk and
nowhere else, the run says "kept on the host only", and `uploaded` is
`false`. That protects against a mistake and a bad release. It does not
protect against losing the host.

The hook is `/etc/scrapeyard/backup-upload`. The contract:

- It is a regular executable file of root's that nobody else can write, in
  directories of which the same is true. A run refuses any other hook before
  it stops anything, because root runs it.
- It is run once per backup, as root, after the application has been
  started again, with one argument: the archive's path. The checksum file is
  that path with `.sha256` added. It is run also when the application did
  not come back ready: that archive may be the last good copy there is.
- It ends with status 0 only when the store has the archive. Any other
  status, or 30 minutes without ending, fails the run: the archive stays on
  the host, and the status file keeps describing the last backup that did
  reach the store.
- It must never replace an object. Archive names carry the second of the
  copy and do not repeat, so a store that already has the name is a fault to
  stop at.
- The store's credential is the hook's own business. Keep it in a file only
  root can read, and give it the right to add objects and nothing else: a
  compromised host must not be able to delete or overwrite the backups it
  made earlier. That is the control. A conditional write such as the
  example's `--if-none-match` is the hook's own courtesy: it keeps an honest
  hook from replacing an object by mistake, and whoever controls the host
  can leave it out. Retention in the store is a rule of the store, not of
  the hook.

No store has been chosen for the first production host, so this example for
an S3-compatible store has not been run against one. Try a hook by hand with
an archive before relying on it:

```bash
#!/bin/bash
# /etc/scrapeyard/backup-upload
set -euo pipefail
archive=$1
# Root only, mode 600: a key that may put objects into this bucket, no more.
export AWS_SHARED_CREDENTIALS_FILE=/etc/scrapeyard/backup-store-credentials
endpoint=https://s3.example.net
bucket=example-scrapeyard-backups
for file in "$archive" "$archive.sha256"; do
  # If-None-Match makes the store refuse a name it already holds.
  aws s3api put-object --endpoint-url "$endpoint" --bucket "$bucket" \
    --key "$(basename -- "$file")" --body "$file" --if-none-match '*' > /dev/null
done
```

A store that does not support conditional writes needs another guard against
replacement: object versioning or an object lock, with a credential that
cannot delete versions. The example stops at the first failure and cannot
be run again for an archive that reached the store: the store refuses the
archive's name the second time. If the archive arrived and its `.sha256` did
not, send the checksum file by hand.

### Testing a restore

An archive that has never been restored is a hope. `restore-test` restores
one on the same host, beside the running deployment and without touching it:

```bash
# On the machine that keeps the key: put the private half on the host's
# memory filesystem, for the length of the test.
ssh <host> 'sudo install -m 600 /dev/stdin /run/scrapeyard-backup.agekey' < scrapeyard-backup.agekey

# On the host:
sudo scrapeyard-backup restore-test \
  /var/backups/scrapeyard/scrapeyard-backup-<UTC time>.tar.zst.age \
  --identity /run/scrapeyard-backup.agekey
sudo rm /run/scrapeyard-backup.agekey
```

While the key is on the host, root on the host, and whoever has taken the
host over, can read every archive made for that key: those on the host,
those in the store, old and new. Remove it when the test ends, whatever the
outcome. A second recipient kept for restore tests does not narrow that,
since it reads the same archives; what it does is keep the recovery key off
every host, so that after a doubt only the test pair is replaced. Archives
made before a replacement stay readable by the old key until they are
deleted.

It refuses an identity file that is not root's alone. What it does:

1. Measures what the archive unpacks to, by decrypting it once without
   writing anything, and requires that and 1 GiB free under
   `/var/backups/scrapeyard`; the disk a test fills is the disk the
   deployment writes to. Then decrypts and unpacks it there, refuses one
   that holds anything but files and directories, and selects the retained
   release named in its `metadata.json`, which must be under
   `/srv/scrapeyard-releases` with its images loaded. A set is checked
   against the SQL of the release that wrote it.
2. Has that release's helper validate the set: every file's size and
   SHA-256, SQLite's integrity check, the migration ledger.
3. Creates the network `<project>-restore-test_backend` with
   `docker network create --internal` on `172.30.1.0/24`, which is not the
   deployment's subnet, and with
   `com.docker.network.bridge.gateway_mode_ipv4=isolated`, which leaves its
   bridge without an address of the host's. It confirms both twice, after
   creating it and again before the application is started: that Docker
   reports it internal, and that the kernel shows no IPv4 address on its
   bridge. That needs Docker 28.0 or later. On a runner (2026-10-13) 28.0.4,
   28.5.2 and 29.9.0 left the bridge without an address and a container on
   it could not reach a listener on the host; 27.5.1 refused the option;
   26.1.4 took it, gave the bridge an address all the same, and the
   container reached the host's listener through it, which is why the
   kernel is asked.
4. Requires the unpacked size and 1 GiB free where Docker keeps its volumes,
   then restores the set into a fresh data volume of the Compose project
   `<project>-restore-test`, and Redis's dump into a fresh Redis volume.
   Redis started with `--appendonly yes`, as the Compose file starts it,
   ignores a bare `dump.rdb` and starts empty (7.4.5). So the dump is loaded
   by a Redis started without the append-only file, the append-only file is
   switched on and written, and that Redis is shut down. The keys it loaded
   plus the keys it found expired must be the number in the archive's
   metadata.
5. Starts the release's Redis and application images on that network, from
   the release's own Compose file, so the application has the confinement it
   has in the deployment: the AppArmor profile, seccomp, no capabilities, a
   read-only root. One file is laid over it (with Compose's `!override`,
   so Compose 2.24.4 or later), and changes only the network,
   the restart policy (`no`, so nothing of the test survives a reboot), the
   retention periods (so the copy removes no history before it is compared),
   the proxy (none: a proxy named by a host name could not be resolved from
   the network, and the application would not start) and the egress probe.
   The application checks the host egress policy at
   startup only while its probe is configured, and the test leaves the probe
   unconfigured rather than install a second firewall policy on a production
   host. What keeps the test's application off the internet is the internal
   network, which the test confirms from inside the container as well: a
   connection to a documentation address must fail with "network is
   unreachable". It also requires the kernel's label of the container's
   first process to be `scrapeyard-chromium (enforce)`, as the deploy
   wrapper does; `docker inspect` only repeats what was asked for.
6. Compares. The restored application, asked exactly as the original was
   once it is ready, must return the same job list and the same digests for
   the witnessed `complete` and `failed` jobs and their results. The jobs
   named in `accepted_after_witness` are left out of the reading and named
   in the report; any other difference fails the test.
   A job's configuration is stored as an encryption envelope and returned
   decrypted by `GET /jobs/{id}`, so equal digests mean the restored
   application decrypted it with the keyring; the test also reads the stored
   value and requires it to be an envelope, and startup itself authenticates
   every envelope before the application serves
   ([SECRET_STORAGE.md](SECRET_STORAGE.md#existing-plaintext-migration)).
7. Removes the project's containers, volumes and network and the unencrypted
   copy, whether it passed, failed or was interrupted, and prints what it
   measured. On a GitHub runner the whole test took 11 to 12 seconds, with
   the copy ready 10 to 11 seconds after the start (2026-10-13).

What the test does not show:

- It decrypts with the keyring in the host's own `deploy.env`. That the copy
  of that file kept elsewhere is complete is shown only by a restore onto
  another host.
- It reads the archive from the host's disk, not from the store.
- A scheduled job whose minute comes while the copy runs is started in the
  copy. It cannot reach its target, but its job then differs from the
  witness and the test fails for that reason. Run the test away from the
  schedules' minutes.
- A name looked up on the test's network was answered by Docker itself
  with a failure, and no query left the host, when that was captured on a
  runner with Docker 29.9.0. Other Docker versions were not tried. Whatever
  becomes of a lookup, no connection can follow it.
- The copy runs beside the deployment with the deployment's own CPU and
  memory limits. It is idle, but run the test when the host is too.
- Jobs submitted in the second between the witness and the stop are in the
  archive and are not compared: the copy runs any of them that were queued,
  and they fail there, unable to reach anything.
- Pending webhook deliveries are attempted by the copy too, and fail at the
  network. On a runner a pending delivery's attempt from the copy ended
  `ConnectError`, and no packet towards its address left the test's network.

### Restoring onto a rebuilt host

`restore` fills the deployment's own two volumes from an archive. It is for
a host that has none: it refuses while `scrapeyard.service` is running, and
while the Compose project has any container or volume, and it never removes
or replaces anything.

1. Rebuild the host: [Host baseline](#host-baseline), the
   [container runtime](#container-runtime-versions), and
   `/srv/scrapeyard-releases` created as root.
2. Extract the release that made the backup under `/srv/scrapeyard-releases`
   as [above](#retained-release-artifacts-and-rollback). Its name is in the
   archive's `metadata.json`; `restore` names it when it is missing.
3. Put `/etc/scrapeyard/deploy.env` back from the copy kept apart from the
   archives (root, mode 600), with the same `COMPOSE_PROJECT_NAME` and the
   keyring the backup was written under.
4. Install the scripts and units from that release, without the `current`
   link and without enabling any unit yet (there is no `current` link to
   read them from, so name the release):

   ```bash
   RELEASE=/srv/scrapeyard-releases/scrapeyard-release-<commit>
   sudo apt-get install age
   sudo install -m 755 "$RELEASE/source/security/manage-release.sh" /usr/local/sbin/scrapeyard-release
   sudo install -m 755 "$RELEASE/source/security/backup.sh" /usr/local/sbin/scrapeyard-backup
   sudo install -m 644 "$RELEASE/source/security/systemd/scrapeyard.service" \
     "$RELEASE/source/security/systemd/scrapeyard-backup.service" \
     "$RELEASE/source/security/systemd/scrapeyard-backup.timer" /etc/systemd/system/
   sudo systemctl daemon-reload
   ```

   Install the recipients file as [above](#installing-it).
5. Fetch the archive and its `.sha256` from the store, check it
   (`sha256sum -c`, and against the checksum recorded elsewhere), and put
   the private key on the host as for a restore test.
6. Restore:

   ```bash
   sudo scrapeyard-backup restore /root/scrapeyard-backup-<UTC time>.tar.zst.age \
     --identity /run/scrapeyard-backup.agekey
   sudo rm /run/scrapeyard-backup.agekey
   ```

   It verifies and loads the release's images, loads the AppArmor profile
   (Compose starts the application's image under it even to copy files),
   validates the set, and fills the two volumes as the restore test does.
   If it fails part of the way, remove the volumes it created
   (`docker volume ls --filter label=com.docker.compose.project=<project>`)
   before running it again.
7. Start the application the way a first installation does, which installs
   the egress policy and starts the restored volumes under it:

   ```bash
   sudo ln -s /srv/scrapeyard-releases/scrapeyard-release-<commit> /srv/scrapeyard-releases/current
   sudo systemctl enable --now scrapeyard.service
   ```

8. Check authenticated `/health/ready` and a known `GET /results/{id}`
   before reconnecting [ingress](#ingress-through-a-tunnel), then enable the
   backup timer and take a backup.

On a GitHub runner (2026-10-13, a few hundred kilobytes of data) step 6
took 40 to 50 seconds, most of it loading the release's images, and the
application was healthy 21 seconds after step 7. Its API then returned the
job list, the jobs and the results exactly as before the backup.

What a restore gives back is the state at the archive's `created_at`. Jobs
accepted after it are gone, so with one backup a day up to a day of them.
Hosts' page counts and cooldowns come back as they were, less those whose
time has run out since. Runs that were queued at the copy are in both the
databases and the queue and are run after the start; a backup waits for an
idle application, so normally there are none.

### What is not covered

- **Off-host copies**, until an upload hook is installed and has been seen to
  work against the chosen store.
- **Alerts.** Nothing tells the operator that a backup failed or that the
  last one is old until monitoring reads the unit's state and
  `completed_at` in the status file ([MONITORING.md](MONITORING.md)).
- **The keyring and credentials**, which are kept by hand as described above.
- **The store's own retention and access**, which are settings of the store.

## Runtime Limits

Set limits to match host capacity:

- `SCRAPEYARD_WORKERS_MAX_CONCURRENT`
- `SCRAPEYARD_WORKERS_MAX_BROWSERS`
- `SCRAPEYARD_WORKERS_MAX_ACCEPTED_RUNS` (default `100`, queued plus running owners)
- `SCRAPEYARD_WORKERS_MEMORY_LIMIT_MB`
- `SCRAPEYARD_WORKERS_BROWSER_MEMORY_RESERVE_MB` (default `512`, per active browser target)
- `SCRAPEYARD_WORKERS_SHUTDOWN_GRACE_SECONDS` (default `30`, shared by worker and webhook shutdown;
  running jobs drain until a quarter of it, at most 5 s, remains, then are
  cancelled and record their failure in that remainder. A cancelled job can
  leave a thread finishing a DNS check, a page parse or a result render, and
  a thread can't be cancelled: the dispatcher and the databases close first,
  and the thread gets what is left of the grace. If it is still running
  then, shutdown reports failure and keeps the instance guard, with the
  databases already closed, and the process exits when the thread returns or
  Compose's `stop_grace_period` kills it. Unresolved worker or background
  tasks, which can still write, keep the databases open as before. The
  grace counts from the stop's signal: the worker polls for no run from
  then on (a start already under way finishes), and Uvicorn's wait for
  requests in flight, at most 5 s (`--timeout-graceful-shutdown 5`), comes
  off it. A request still running then is cut off without an answer)
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
- `SCRAPEYARD_API_RESULT_THREAD_MAX_WORKERS` (default `2`; bounds result responses
  from artifact load through rendering and sending, so at most this many
  rendered bodies are in memory at once; a body still unsent after 20 s, such
  as one to a stalled client, stops counting so it cannot hold a slot
  indefinitely, and a transfer that makes no progress for 30 s is abandoned
  and its connection closed, which frees the body)
- `SCRAPEYARD_TRANSFORM_REGEX_TIMEOUT_SECONDS` (default `0.1` per operation)
- `SCRAPEYARD_TRANSFORM_REGEX_MAX_PATTERN_BYTES` (default `2048` UTF-8 bytes)
- `SCRAPEYARD_TRANSFORM_MAX_PIPELINE_STEPS` (default `32`)
- `SCRAPEYARD_TRANSFORM_MAX_VALUE_BYTES` (default `1048576`, 1 MiB per intermediate value)
- `SCRAPEYARD_WEBHOOK_MAX_DELIVERY_ATTEMPTS` (default `5`, at most `100`)
- `SCRAPEYARD_WEBHOOK_MAX_DELIVERY_AGE_SECONDS` (default `86400`, at most 30 days)
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
including browser subprocesses, active file cache, and kernel charges, less the
group's inactive file cache (`inactive_file` on v2, `total_inactive_file` on v1),
which the kernel reclaims before real memory pressure. Without a readable
`memory.stat`, the full usage counts. Both
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
checks. The worker samples memory at most once per poll and only when it has a
free slot, so it counts at most one deferral per poll; the metrics refresh
samples headroom too, keeping the gauge current while the worker is full.
Existing active-work and queue-age metrics show occupancy and delay.

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
Explicit job deletion remains filesystem-first and retryable, and it also
releases SQLite while it removes directories. Running
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
larger files are reported as corrupt without being loaded. Each process parses
a retained `results.json` once: while its inode, size, and modification and
change times stay the same, later passes only check that it is still a regular
file. Replacing or modifying the file changes at least one of those, so it is
parsed again; media corruption that changes none of them is found after the
next restart or when the result is read. A run-tree snapshot
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
Response handling streams the status and headers, then reads at most 64 KiB of
the unused body, undecoded and discarded, so the connection can be reused; a
longer body closes the connection. Destination-controlled response content is
never buffered. The webhook `timeout` bounds each connect, write and read, and
also the whole exchange: a receiver that trickles bytes fails the attempt as a
retryable transport timeout. A status that arrived in time keeps its outcome
even if the body is still arriving at the deadline.

For 429 and 503 only, `Retry-After` accepts either non-negative integer delta
seconds (ASCII digits) or a future timezone-aware HTTP date. Malformed, negative, past, or
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
