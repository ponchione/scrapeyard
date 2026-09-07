# Target-scoped fetch sessions

Each `scrape_target` call owns its HTTP client or browser context through the
first page and pagination. Successful pages share cookies; browser pages also
share origin-scoped local storage. Each browser fetch creates and closes a new
page, with fresh diagnostics and route callbacks bound to that page's budget.
Targets, runs, and validation rescrapes receive separate sessions.

An unsuccessful fetch attempt discards its session before `RetryHandler` applies
the existing retry/backoff policy. Disconnected browsers are retryable transport
failures. Retries start with empty cookies and browser storage. Success, failure,
cancellation (including during startup), and budget exhaustion close owned
resources before returning and releasing the worker's browser slot. CDP cleanup
closes only the owned context and driver connection, leaving the external browser
running.

Basic HTTP retains at most one pool, keyed by logical origin, validated connection
origin, and proxy. Changing any key closes the previous pool. DNS validation,
pinned-IP/Host/SNI routing, redirect checks, byte limits, and rate limiting still
run on every request. Cookies use the logical URL's domain/path rules; HTTPX's
IP-address cookie jar never routes them. Browser headers remain scoped to the
original target origin when pagination visits another origin.

There is no process-wide pool or new service setting. Browser launch options come
from the pinned Scrapling 0.2 engines, including Chromium sandbox/stealth settings
and Camoufox options. The adapter uses their private configuration helpers; any
Scrapling upgrade must pass the real-browser smoke lane.

## Controlled benchmark, 2026-09-07

Compared baseline `1156d39` with this change, using
`scripts/benchmark_fetch_sessions.py`: one target, five sequential HTML pages,
five verified extracted titles, exhaustive pagination, no retries, no rate-limit
delay. Each trial ran in a fresh 4 GiB container with 1 GiB shared memory and the
same read-only runtime/browser image
`sha256:3c46d275b4d21f778866d2015017f1b874060ba6346033ccc10cee697f4eb50d`.
Only the mounted application source differed. Three trials per case, with warm
host image caches. The fixture used HTTP/1.1 keep-alive with TCP_NODELAY on an
isolated public-address Docker network; no destination checks were patched.

| Fetcher | Median seconds, before → after | TCP connections, before → after | Browser launches, before → after | Median peak container MiB, before → after |
| --- | --- | --- | --- | --- |
| Basic | 0.258 → 0.184 (29% less) | 5 → 1 | 0 → 0 | 110.5 → 109.1 |
| Chromium | 2.310 → 0.719 (69% less) | 5–7 → 1 | 5 → 1 | 304.8 → 308.1 |
| Camoufox | 7.042 → 2.078 (70% less) | 5 → 1 | 5 → 1 | 423.3 → 521.5 |

Elapsed samples (seconds), before / after:

- Basic: `[0.258, 0.255, 0.260]` / `[0.186, 0.184, 0.176]`.
- Chromium: `[2.310, 2.255, 2.346]` / `[0.719, 0.713, 0.782]`.
- Camoufox: `[7.098, 7.042, 6.889]` / `[2.078, 2.147, 2.069]`.

Connections count accepted sockets, including browser preconnects. Launch counts
wrap the real Playwright/Rebrowser launch methods. Peak memory comes from cgroup
`memory.peak` and includes the fixture server and child browser processes.
Camoufox's retained context increased median peak memory by 23%; its highest
observed after-change peak was 552.3 MiB. Keep the existing admission/memory limits.
These results establish reduced setup cost for this controlled workload; they
do not qualify retailer throughput or the joint Eyebox operating envelope.

To reproduce with a built browser-capable image and the repository's installed
Chromium AppArmor profile:

```bash
docker network create --internal --subnet 203.66.82.0/28 scrapeyard-fetch-benchmark
docker run --rm --init --network scrapeyard-fetch-benchmark --ip 203.66.82.2 \
  --add-host fixture.public.test:203.66.82.2 \
  --security-opt apparmor=scrapeyard-chromium \
  --security-opt seccomp=security/seccomp/chromium.json \
  --memory 4g --shm-size 1g --entrypoint python \
  -v "$PWD:/repo:ro" -e PYTHONPATH=/repo/src scrapeyard:local \
  /repo/scripts/benchmark_fetch_sessions.py --fetcher dynamic
docker network rm scrapeyard-fetch-benchmark
```

Repeat with `--fetcher basic` and `--fetcher stealthy`; retain each JSON output.
For baseline comparisons, mount that revision's `src` directory and select it
with `PYTHONPATH`, retaining the same fixture script and image.

## Regression checks

`tests/unit/test_fetch_sessions.py` exercises real TCP reuse across pagination,
two logical hosts pinned to one IP, cookie isolation between targets, retry state
reset, response/socket closure, byte exhaustion, and repeated cancellation.
Existing streaming, redirect, rate-limit, pagination, and budget checks still run.

`tests/smoke/verify_fetch_sessions.py` runs in the container/browser smoke lane for
Chromium, stealth Chromium, and Camoufox. It checks cookie/local-storage reuse,
cross-origin and cross-target isolation, fresh per-page budget/diagnostic binding,
private subrequest rejection, disconnected-browser retries, and cleanup during
page actions and browser startup. The lane also retains its existing real
browser actions, screenshots, redirects, egress checks, and graceful shutdown.

Validation for this change: `poetry check --lock`, Ruff over `src tests scripts`,
and strict mypy over all 95 source files passed. `poetry run pytest -W error`
passed 2,081 tests with 16 live-Redis skips and 88.50% coverage. The production
image built, and the container/browser smoke completed in 78 seconds, including
all three session-check modes and four validated screenshots.

Local smoke environment: an existing deployment occupied the default backend
subnet, and this host lacked bridge netfilter. A disposable checkout relocated
test backend addresses to `172.31.13.0/24` and attached the same generated egress
chain to `OUTPUT` in the test application's network namespace before startup.
Private/metadata denial, policy attestation, browser guards, and sandbox checks
remained enforced. This validates browser behavior with enforced egress; it does
not qualify this host's missing bridge hook. These environment adjustments are
not part of the application or committed smoke configuration.
