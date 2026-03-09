# Technical Debt Register

Date: 2026-03-08

## TD-001: WO-011 Queue Backend Not arq-Based

### Summary
The project currently uses a custom in-memory queue implementation (`asyncio.PriorityQueue`) instead of an `arq`-based worker/queue path required by WO-011 and referenced by the spec.

### Current State
- Queue implementation: custom in-memory worker pool in `src/scrapeyard/queue/pool.py`
- Worker execution: `scrape_task` in `src/scrapeyard/queue/worker.py`
- Functionality status: usable and operational for single-instance usage
- Compliance status: partial/non-compliant with WO-011 requirement for arq-based queueing

### Why Deferred
- The system is currently usable and passes core unit validations for queue/worker behavior.
- This gap is architectural/compliance debt, not an immediate functional blocker.
- Implementing arq properly requires an explicit backend decision (Redis-backed arq vs. compatibility adapter approach).

### Impact
- Does not block basic API usage (`/scrape`, `/jobs`, `/results`, `/errors`).
- Leaves a deviation from work-order/spec expectations.
- May increase future refactor cost if additional queue features are built on top of current custom backend.

### Exit Criteria
This debt is resolved when all are true:
1. Queue backend is arq-based (or formally approved equivalent with documented rationale).
2. Priority ordering (`high > normal > low`) remains correct.
3. Memory guard, concurrency, browser limits, and worker semantics remain intact.
4. Lifespan startup/shutdown manages queue worker cleanly.
5. Automated tests cover enqueue/dispatch behavior and pass.

### Target
Defer to next infrastructure-focused iteration before adding new queue-dependent features.
