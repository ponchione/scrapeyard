# Technical Debt Register

Last updated: 2026-03-24

This file tracks active technical debt only. Resolved items from earlier audits
have been removed rather than kept as historical clutter.

Audit basis:
- Full codebase audit against epics 1-6 (run model elevation, API contract)
  and proxy/rate-limiter spec
- Current tests: `279 passed` across unit + integration

No active tech debt items remain open.

Resolved in 2026-03-18 round:
- durable Redis-backed queueing replaced the in-memory queue path
- all ad hoc execution now goes through the queue with sync-wait semantics
- cleanup retention now delegates through the `ResultStore` contract
- browser tuning and `adaptive_domain` are now first-class config fields

Resolved in 2026-03-20 round (epics 1-6):
- `formatters/` module removed; output is JSON-only with inline grouping
- `JobRun` model and `job_runs` table added; run lifecycle tracked end-to-end
- `run_id` wired through worker, error store, result store, and API responses
- `trigger` parameter (adhoc/scheduled) propagated from scheduler through worker
- API contract expanded: job detail with runs, stats derived via LEFT JOIN
- `get_next_run_time()` exposed via scheduler service

Resolved in 2026-03-23 round (proxy & rate limiting):
- managed proxy routing with three-level precedence (target > job > service env)
- cross-job domain rate limiting via Redis (`RedisDomainRateLimiter`)
- `LocalDomainRateLimiter` fallback for testing and single-job scenarios
- proxy credential redaction in all log output

Add new entries here only when fresh active debt is identified.
