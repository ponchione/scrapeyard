# Issue 004: Aggressive Redis Polling For Sync `/scrape`

Severity: Medium

## Summary

Synchronous `/scrape` requests poll job completion every 50 ms. That is much more aggressive than needed for a user-facing wait path.

## Evidence

- `src/scrapeyard/api/routes.py:81` calls `queued_job.result(timeout=..., poll_delay=0.05)`.
- The installed `arq.jobs.Job.result()` implementation polls Redis once per `poll_delay`, so this setting directly drives Redis traffic.

## Why It Matters

- Each waiting client generates roughly 20 Redis polls per second.
- Concurrent sync requests create avoidable Redis load without improving scrape speed.
- The system already has a queue boundary, so a slightly slower poll interval is usually acceptable.

## Recommendation

- Increase the poll interval substantially, for example to `0.25` to `0.5` seconds.
- Consider making the poll delay configurable through settings.
- If sync mode matters heavily, measure real user tolerance and Redis load before finalizing.

## Deployment Risk

Medium. This is unlikely to break correctness, but it is wasteful and easy to fix.
