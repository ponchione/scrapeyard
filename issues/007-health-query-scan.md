# Issue 007: Health Endpoint Uses Repeated Aggregate Scan

Severity: Low

## Summary

The health endpoint periodically runs an aggregate query over the whole `jobs` table, but the table lacks an index aligned with that query pattern.

## Evidence

- `src/scrapeyard/main.py:97` runs `SELECT project, status, COUNT(*) FROM jobs GROUP BY project, status`.
- `src/scrapeyard/main.py:27` and `src/scrapeyard/main.py:92` reduce frequency with a 5-second in-memory cache.
- `sql/001_create_jobs.sql` defines no supporting index for `(project, status)`.

## Why It Matters

- Frequent liveness or readiness probes can still trigger repeated work.
- As the job table grows, the cost of this query will grow with it.
- The cache helps, but it does not remove the fundamental table-scan pattern.

## Recommendation

- Add an index on `(project, status)` if this query stays.
- If probe volume is high, move project summary generation to a cached background refresh rather than on-demand request work.

## Deployment Risk

Low today, but likely to become visible with long retention and frequent health checks.
