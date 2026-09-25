# HTTP API Contract

## Version and compatibility policy

The unprefixed HTTP paths expose Scrapeyard API contract version `1`. Every
HTTP response, including middleware rejections, carries
`X-Scrapeyard-API-Version: 1`. Additive fields and new endpoints may be added
within version 1. Removing or renaming a field, changing its meaning/type, or
moving records requires a new negotiated or path-prefixed API version.

Result-envelope cleanup is the one v1 migration from the historical
pre-contract format. Eyebox and other existing readers can request
`compatibility=legacy-v0` on both `POST /scrape` and `GET /results/{job_id}`.
That mode preserves the old API-envelope → stored-document → `results` nesting.
It will not be removed without a documented major API version, a migration
notice, and at least a 90-day overlap. New clients should omit the parameter.

## Admission overload

`POST /scrape` and `POST /jobs/{job_id}/trigger` return `503` with
`Retry-After: 5` when the accepted-run cap or memory ceiling prevents admission.
The response uses the normal `service_unavailable` error envelope. Retry the
same YAML and `Idempotency-Key` after the delay: rejection creates no new job,
queued snapshot, or idempotency record. An existing idempotent request still
replays its accepted job/run while the service is full.

The cap counts current queued and running run owners across ad-hoc, manual, and
scheduled submissions. Idle schedule definitions and terminal jobs do not count.
Completion, failure, cancellation, deletion, or submission rollback release the
slot. Delivery recovery keeps the original reservation. Scheduled overload is
recorded durably as `admission_backlog` or `admission_memory` and retried at the
next cron fire. Memory pressure can delay already-queued execution; cancellation
and recovery remain available.

## Result responses

The v1 terminal sync response and polling response use the same shape:

```json
{
  "job_id": "...",
  "run_id": "...",
  "status": "complete",
  "completed_at": "2026-07-12T16:00:00Z",
  "errors": [],
  "targets": [],
  "budget_error": null,
  "results": []
}
```

`results` is the only record location. It is a list for `output.group_by:
merge`, and a mapping of target group names to target result/data objects for
`output.group_by: target`. Run/job/status metadata is never repeated inside
that field. Each merge-mode record includes an injected `_source` field with
the target hostname (and port, when present), so `_source` is reserved as a
selector field name when merge grouping is configured. `targets` contains
bounded target diagnostics; secrets remain
redacted and local result paths are never serialized. Diagnostic URLs redact
all query values and the complete fragment. Selector-engine diagnostics expose
a query SHA-256 fingerprint and exception type, not the raw query or exception
message.

`execution.fail_strategy: all_or_nothing` is strict: if any target fails,
`status` is `failed`, `results` is empty in either grouping mode, and the run,
artifact metadata, and webhook `result_count` are all zero. Per-target status,
errors, page counts, and redacted debug details remain under `targets`.
`targets[].count` is the number of accepted records (zero for a rejected atomic
run); `targets[].observed_count` records how many records extraction observed
for diagnostics. Fully successful atomic runs publish all records normally.

### Pagination coverage

Job `status: complete` describes execution success, including successful bounded
samples. Each target summary (and target-group result) includes `pagination`:

```json
{"stop_reason": "max_pages", "exhausted": false}
```

| Stop reason | Evidence |
| --- | --- |
| `exhausted` | The configured next-page selector matched nothing on the last scraped page, a page-parameter page extracted no records, or a click-mode next control was missing, hidden or disabled; `exhausted` is `true`. |
| `max_pages` | A next link (page-parameter: a further page; click mode: an enabled next control) remains at the page cap. Its destination is not fetched or DNS-validated. |
| `repeated_url` | The next URL, or its redirect destination, was already visited. |
| `repeated_page` | A page-parameter or click-mode page returned exactly the records of an earlier page, or a click left the items unchanged. |
| `unsafe_next_url` | Destination validation rejected the next link. |
| `invalid_next_link` | A selected next-page element has no usable href. |
| `domain_guard` | Scrapeyard stopped before requesting the next page: the host's daily page budget is used or it is cooling down after an access denial. The reason is appended to the target's `errors`. |
| `cache_miss` | Page-cache replay has no recording for the next page (or click page); no request was made. |
| `not_configured` | No pagination configuration; coverage is unknown. |
| `unknown` | No conclusive pagination evidence, including interrupted/failed extraction. |

Only `exhausted` sets the boolean to `true`. The cap takes precedence over URL
safety/loop checks for an unfetched next link. The final allowed page is inspected
even when `max_pages: 1`. All returned records remain usable under the existing
execution/failure policy. Older artifacts can lack `pagination`; treat that as
unknown coverage.

Exhausting a selector does not establish retailer or category completeness.
Consumers must declare which target set is an exhaustive business snapshot and
require successful, error-free, exhausted results for every target contributing
to a removal scope. A capped, sampled, failed, looping, or unknown target cannot
authorize absence-based listing removals. Eyebox's declaration is documented in
its [deployment topology](../../eyebox/docs/deployment-topology.md#listing-removal-coverage).
The serialized `tests/fixtures/pagination-coverage.json` contract fixture is
mirrored in Eyebox's `ingest/tests/fixtures/`; update both copies together.

The artifact on disk retains its historical self-describing document so old
backups and pending webhooks remain readable. The API serializer unwraps that
document at the boundary. The `legacy-v0` compatibility parameter exposes it
unchanged during client migration.

An accepted async request, an accepted manual trigger, or a sync request that
reaches its HTTP wait limit returns `202` with the persisted `job_id`, `run_id`,
current persisted status, and poll URL. A timeout never rewrites `running` to
`queued`, and the queue handler returning is only a wake-up signal: the exact
durable run must have a result-bearing terminal status before the API reads its
artifact or returns `200`. The configured wait is a hard monotonic deadline;
polling sleep and queue-state reads do not extend it or select a state first
observed after it expired.

### Page cache and domain guard outcomes

When a job sets `execution.page_cache` to `record` or `replay`, the result has a
top-level `page_cache` field with that mode; the field is absent otherwise. In
replay, each `targets[]` entry carries `recorded_at`, the earliest recording
time of the pages it was built from. Replayed results are reconstructed from
stored HTML and are not a live observation of the site; do not treat them as
current prices, availability, or listing coverage.

Stops that Scrapeyard imposes on itself before any request have their own
`targets[].error_type` values and are not retried or counted against the
domain circuit breaker:

| Error type | Meaning |
| --- | --- |
| `domain_daily_limit` | The host used its daily top-level page budget (UTC day). |
| `domain_cooldown` | The host denied access recently and is cooling down. |
| `cache_miss` | Replay has no recording for the page, or the service has no page cache directory. |

Runs executed with a domain guard report `run_budget.domain_guard`:

```json
{
  "daily_page_limit": 200,
  "cooldown_seconds": 21600,
  "pages_admitted": {"shop.example.test": 12},
  "daily_limit_stops": {},
  "cooldown_stops": {"other.example.test": 1},
  "cooldowns_started": []
}
```

### Traffic by host

Every run reports `run_budget.traffic`, the requests it sent grouped by host:

```json
{
  "requests": 812,
  "blocked": 3401,
  "cached": 400,
  "bytes": 5210044,
  "first_party": {"hosts": 2, "requests": 640, "blocked": 3100, "cached": 380,
                  "bytes": 4900312},
  "third_party": {"hosts": 14, "requests": 172, "blocked": 301, "cached": 20,
                  "bytes": 309732},
  "resource_types": {
    "document": {"requests": 30, "blocked": 0, "cached": 0, "bytes": 1802113},
    "script": {"requests": 505, "blocked": 12, "cached": 400, "bytes": 3015880},
    "xhr": {"requests": 180, "blocked": 0, "cached": 0, "bytes": 290051},
    "fetch": {"requests": 90, "blocked": 0, "cached": 0, "bytes": 96000},
    "other": {"requests": 7, "blocked": 3389, "cached": 0, "bytes": 6000}
  },
  "hosts": [
    {"host": "www.example.test", "third_party": false, "requests": 610,
     "blocked": 3050, "cached": 370, "bytes": 4700120,
     "resource_types": {
       "document": {"requests": 30, "blocked": 0, "cached": 0, "bytes": 1802113},
       "script": {"requests": 400, "blocked": 0, "cached": 370, "bytes": 2607956},
       "xhr": {"requests": 180, "blocked": 0, "cached": 0, "bytes": 290051},
       "other": {"requests": 0, "blocked": 3050, "cached": 0, "bytes": 0}
     }},
    {"host": "tags.example-ads.test", "third_party": true, "requests": 96,
     "blocked": 0, "cached": 20, "bytes": 180211,
     "resource_types": {
       "script": {"requests": 96, "blocked": 0, "cached": 20, "bytes": 180211}
     }}
  ],
  "hosts_omitted": 3
}
```

- `requests` are requests released to the network, including native redirect
  hops and basic-fetch retries; the total matches `run_budget.requests`.
- `blocked` are browser requests aborted before sending: resource types dropped
  by `browser.disable_resources`, subrequests dropped by
  `browser.block_third_party` or `browser.block_url_patterns`, and URL-guard
  rejections.
- `cached` are browser requests answered from the run's script cache
  (`execution.reuse_browser`) after every guard admitted them. Nothing is sent,
  so they are not in `requests` and add no `bytes`.
- `bytes` are response bytes received. For browser requests: the response
  header block (estimated from its lines) plus the encoded (compressed) body,
  taken from `Content-Length`, or measured by the browser once the response
  finishes when it declares no length. For basic fetches: the encoded body.
  Responses still in flight when a page closes may be missing.
- A host is `third_party` when its registrable domain (public suffix plus one
  label, from the bundled suffix list; the last two labels for unlisted suffixes
  such as `.test`) differs from the requesting target URL's. A host that is
  first-party for any target of the run is first-party.
- `hosts` lists at most 25 hosts, ordered by requests, bytes, blocked, cached;
  `hosts_omitted` counts the rest, which the totals still include. A run tracks
  at most 1,000 distinct hosts; later hosts are pooled as `(other first-party
  hosts)` or `(other third-party hosts)`.
- `resource_types` splits the same counts by the browser's resource type:
  `document` (pages, frames and basic fetches), `script`, `xhr`, `fetch`, and
  `other` for everything else (stylesheets, images, fonts, media, beacons and
  unknown types). The run totals always list all five types; each host lists
  only the types it had.
- Only hostnames are recorded, never paths or query strings.

## Error envelope

Routes, FastAPI request validation, authentication, body-size enforcement, and
rate limiting all return:

```json
{
  "error": "Human-readable summary",
  "code": "machine_readable_code",
  "status_code": 422,
  "details": [
    {
      "location": ["query", "offset"],
      "message": "Input should be greater than or equal to 0",
      "type": "greater_than_equal"
    }
  ]
}
```

`details` is always a list and is empty when no safe field-level detail exists.
Validation details omit submitted values and URLs that could contain secrets.
The legacy string `error` field remains in place as an additive migration aid.
Framework-generated errors, including unmatched routes (`404`) and unsupported
methods (`405`), use this same envelope. Worker-quiescence timeouts use `504`
with code `gateway_timeout`; all three statuses are declared in OpenAPI.

## Pagination

`GET /jobs` and `GET /errors` continue returning arrays. Pagination metadata
remains in the existing response headers to avoid breaking Eyebox:

- `X-Scrapeyard-Limit`
- `X-Scrapeyard-Offset`
- `X-Scrapeyard-Item-Count`
- `X-Scrapeyard-Has-More`
- `X-Scrapeyard-Next-Offset` when another page exists

The headers are declared in OpenAPI. A future body-envelope migration would
require a new API version.

## OpenAPI

Every success response has a concrete Pydantic schema, including distinct
`200` terminal and `202` queued scrape/result responses. Every documented
route error uses the shared error schema. The two `204` lifecycle operations
intentionally have no response body.

## Operations endpoints

`GET /health` and `GET /health/live` are public, minimal process-liveness
responses. `GET /health/ready` and `GET /metrics` require the `health-detail`
scope. Readiness probes every durable subsystem and required background task;
metrics use Prometheus text exposition with bounded labels. See
[MONITORING.md](MONITORING.md) for probe semantics, scrape configuration, and
alert guidance.

When `SCRAPEYARD_UNTRUSTED_SUBMISSIONS=true`, submitted job-level/target-level
proxies and `browser.cdp_url` require the separate `transport-admin` scope in
addition to the route's normal `submit` or `schedule-admin` scope. Ordinary
callers use the operator-configured service proxy. This authorization check
does not replace the mandatory connected-IP deployment policy.

### Domain guard state

`GET /domains/{host}/guard` shows a host's top-level page count for the current
UTC day and any active access-denial cooldown. `DELETE /domains/{host}/guard`
clears both and returns the resulting state. `{host}` is a bare hostname
(a leading `www.` and any port are ignored, matching how target URLs are
counted); anything else returns `400`. Guard state is global, so both routes
require the `transport-admin` scope on a credential without a project
restriction; project-scoped callers receive `403`.

```json
{
  "host": "shop.example.test",
  "day": "20260101",
  "pages_today": 12,
  "daily_page_limit": 200,
  "cooldown_seconds": 21600,
  "cooldown_active": true,
  "cooldown_remaining_seconds": 5021.4,
  "cooldown_until": "2026-01-01T14:03:11+00:00"
}
```

`daily_page_limit` is the service limit (`0` = unlimited); a job's
`execution.domain_daily_page_limit` can only lower it for that job's runs.
Submitting `execution.page_cache: record` or `replay` returns `422` when
`SCRAPEYARD_PAGE_CACHE_DIR` is not configured.
