# Implement Real Queue Priority

> **Status: completed for 0.6.0.** This file is a historical design record;
> its Problem section describes the pre-implementation state. See the
> [archive index](README.md) for completion evidence.

Priority: P2

## Problem

High, normal, and low priority currently adjust arq defer time by only minus one second, zero, or plus one second. Under sustained backlog this is a timing bias, not priority ordering: a high-priority job submitted later remains behind sufficiently older normal and low jobs.

Relevant code:

- `src/scrapeyard/queue/pool.py`
- `src/scrapeyard/config/schema.py`
- `src/scrapeyard/scheduler/cron.py`
- `src/scrapeyard/api/scrape_submission.py`

## Required Outcome

The documented priority field must produce predictable ordering under backlog without starving lower-priority jobs.

## Implementation Scope

1. Choose and document a real priority mechanism compatible with arq, such as separate Redis queues with weighted polling or a priority-aware dispatcher.
2. Preserve one logical job identity and result-wait behavior across queues.
3. Define fairness/aging so low-priority work eventually runs.
4. Expose queue depth by priority in metrics/health diagnostics.
5. Remove the defer-time offset implementation once migration is complete.

## Acceptance Criteria

- A later high-priority job overtakes queued normal/low work according to the documented policy.
- Low-priority jobs cannot starve indefinitely.
- Sync waiting works regardless of queue.
- Duplicate `run_id` enqueue protection remains intact.
- Scheduled and ad-hoc submissions use the same priority semantics.

## Verification

- Add deterministic unit tests for ordering and fairness.
- Add a live-Redis backlog test across all three priorities.
- Run the queue, scheduler, integration, and full suites.

## Non-Goals

- Priority does not preempt a target that has already started.
