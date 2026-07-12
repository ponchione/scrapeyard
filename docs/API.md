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
that field. `targets` contains bounded target diagnostics; secrets remain
redacted and local result paths are never serialized.

The artifact on disk retains its historical self-describing document so old
backups and pending webhooks remain readable. The API serializer unwraps that
document at the boundary. The `legacy-v0` compatibility parameter exposes it
unchanged during client migration.

An accepted async request, an accepted manual trigger, or a sync request that
reaches its HTTP wait limit returns `202` with the persisted `job_id`, `run_id`,
current persisted status, and poll URL. A timeout never rewrites `running` to
`queued`.

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
