# Issue 005: Webhook Dispatch Lacks Connection Reuse And Task Tracking

Severity: Medium

## Summary

Webhook delivery spins up a fresh HTTP client for every request and dispatches it via detached background tasks.

## Evidence

- `src/scrapeyard/queue/worker.py:407` creates fire-and-forget tasks with `asyncio.create_task(...)`.
- `src/scrapeyard/webhook/dispatcher.py:36` creates a new `httpx.AsyncClient()` per dispatch.

## Why It Matters

- Repeated client creation throws away connection pooling and TLS reuse.
- Slow or failing webhook destinations can accumulate untracked background tasks.
- This becomes more visible once job throughput rises or webhook destinations are unstable.

## Recommendation

- Promote `HttpWebhookDispatcher` to own a long-lived shared `httpx.AsyncClient`.
- Track outstanding webhook tasks so shutdown can await or cancel them cleanly.
- Add backpressure limits if webhook volume can spike.

## Deployment Risk

Medium. It is mostly a throughput and resource-efficiency problem.
