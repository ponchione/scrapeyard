# Issue 008: Dead-Code And Cleanup Candidates

Severity: Low

## Summary

A small amount of code appears unused in production paths. This is not a direct performance problem, but it adds maintenance noise before deployment.

## Evidence

- `src/scrapeyard/queue/pool.py:76` exposes `can_accept()`, but current callers appear to use `_check_memory()` indirectly through `enqueue(...)` instead.
- `src/scrapeyard/engine/selectors.py:20` defines `extract_item_selectors()`, and the current references appear to be test-only.

## Why It Matters

- Extra code paths make auditing and future optimization harder.
- Unused helpers can hide stale assumptions about intended behavior.

## Recommendation

- Confirm whether these helpers are part of intended public API surface.
- Remove or deprecate them if they are not needed.
- Keep the codebase lean before deployment to reduce future audit cost.

## Deployment Risk

Low. This is cleanup, not a blocker.
