# Technical Debt Register

Last updated: 2026-03-18

This file tracks active technical debt only. Resolved items from earlier audits
have been removed rather than kept as historical clutter.

Audit basis:
- Manual review of the current `src/` tree after the Brownells webhook and
  item-scoped extraction work
- Current tests in `tests/`
- `./.venv/bin/python -m pytest -q` with `197 passed`

No active items from the 2026-03-18 remediation streams remain open.

Resolved in this round:
- durable Redis-backed queueing replaced the in-memory queue path
- all ad hoc execution now goes through the queue with sync-wait semantics
- cleanup retention now delegates through the `ResultStore` contract
- browser tuning and `adaptive_domain` are now first-class config fields

Add new entries here only when fresh active debt is identified.
