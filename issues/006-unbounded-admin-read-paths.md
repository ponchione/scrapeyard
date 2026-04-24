# Issue 006: Unbounded Admin Read Paths

Severity: Medium

## Summary

Admin-style endpoints return entire result sets without pagination or hard limits.

## Evidence

- `src/scrapeyard/api/routes.py:225` returns all rows from `list_jobs_with_stats(...)`.
- `src/scrapeyard/api/routes.py:401` returns all rows from `query_errors(...)`.
- `src/scrapeyard/storage/error_store.py:93` fetches every matching error row.
- `src/scrapeyard/storage/job_store.py:165` performs a full aggregate join to build job stats.

## Why It Matters

- Large job histories will increase latency and memory use for simple list views.
- `/errors` in particular can become a heavy operational endpoint during outages, exactly when responsiveness matters most.
- This makes the service harder to operate safely at scale.

## Recommendation

- Add pagination and explicit limits to `/jobs` and `/errors`.
- Add descending default ordering plus cursor or offset parameters.
- Return summary views by default and reserve full-history fetches for explicit requests.

## Deployment Risk

Medium. This becomes more painful over time rather than immediately.
