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
| `exhausted` | The configured next-page selector matched no links on the last scraped page; `exhausted` is `true`. |
| `max_pages` | A next link remains at the page cap. Its destination is not fetched or DNS-validated. |
| `repeated_url` | The next URL, or its redirect destination, was already visited. |
| `unsafe_next_url` | Destination validation rejected the next link. |
| `invalid_next_link` | A selected next-page element has no usable href. |
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
