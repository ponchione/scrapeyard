# Bound and Operationalize Webhook Retries

Priority: P1

## Problem

Retryable webhook failures remain pending forever with a capped delay, and startup schedules one task for every pending delivery. A persistently failing endpoint can create an unbounded backlog, large task counts, frequent writes, and permanent secret retention.

Relevant code:

- `src/scrapeyard/webhook/dispatcher.py`
- `src/scrapeyard/storage/webhook_outbox.py`
- `sql/009_create_webhook_outbox.sql`
- `src/scrapeyard/common/settings.py`

## Required Outcome

Webhook delivery must have configurable attempt/age limits, bounded dispatch concurrency, inspectable dead-letter state, and controlled retention.

## Implementation Scope

1. Add service settings for maximum attempts, maximum delivery age, dispatch concurrency, startup batch size, and delivered/failed retention.
2. Mark exhausted deliveries permanently failed with a precise reason.
3. Replace one-task-per-row startup replay with a bounded dispatcher loop or worker pool.
4. Add queries or API/metrics support for pending, delivered, failed, oldest-pending age, and attempts.
5. Add cleanup for old delivered and permanently failed rows.
6. Honor `Retry-After` for 429 and appropriate 503 responses when safely parseable.

## Acceptance Criteria

- A permanently retryable endpoint stops receiving attempts after the configured limit.
- Startup memory/task count is bounded regardless of outbox size.
- Failed deliveries remain inspectable for the configured retention window.
- Shutdown and restart preserve pending work without duplicate concurrent dispatch.
- Headers and error strings remain redacted in logs.

## Verification

- Add tests for attempt exhaustion, age exhaustion, concurrency, batching, retry-after, cleanup, restart, and shutdown.
- Add migration and store tests for any schema changes.
- Run the full webhook and integration suites.

## Non-Goals

- Do not make job completion wait for external webhook success.
