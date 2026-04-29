# Issue 008: ~~Dead-Code And Cleanup Candidates~~ (RESOLVED)

Severity: Low

Status: Resolved

## Summary

A small amount of code appeared unused in production paths. This was not a direct performance problem, but it added maintenance noise before deployment.

## Evidence

- `src/scrapeyard/queue/pool.py` exposed `can_accept()`, but callers use memory admission through `enqueue(...)`.
- `src/scrapeyard/engine/selectors.py` defined `extract_item_selectors()`, and the references were test-only.

## Why It Matters

- Extra code paths make auditing and future optimization harder.
- Unused helpers can hide stale assumptions about intended behavior.

## Recommendation

- Removed the unused helpers from the production surface.
- Kept focused selector and worker-pool tests on the remaining supported helpers.

## Deployment Risk

Low. This is cleanup, not a blocker.

## Resolution

Resolved by removing the `WorkerPool.can_accept()` wrapper and keeping `enqueue(...)` on the direct memory check path. The stale selector helper is no longer present in the codebase.
