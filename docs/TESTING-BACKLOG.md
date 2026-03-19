# Testing Backlog

Last updated: 2026-03-19

This file tracks the remaining automated testing work after the live Redis lane
was added and the Eyeboxapp Brownells validation was completed.

## Completed Recently

- Fast regression suite remains in place
- Live Redis queue-path automation added in `tests/live_redis/`
- Brownells two-phase Eyeboxapp validation completed manually
- Adaptive browser regression fixed and covered by unit tests

## Remaining Automated Test Items

### 1. Browser System Test Lane

Priority: high

Add a deterministic Docker-based system test that exercises the real browser
path with:

- local fixture target pages
- real `fetcher: dynamic`
- item-scoped extraction
- pagination across multiple pages
- local webhook sink

Why it matters:

- browser-path regressions are not fully covered by the fast suite
- the Brownells Phase 2 bug lived in this layer
- this should catch Scrapling / Playwright / Chromium compatibility issues

### 2. Webhook End-to-End Automation

Priority: medium-high

Add an automated test that verifies webhook delivery from Scrapeyard to a local
HTTP sink without monkeypatching the dispatcher.

Why it matters:

- current webhook coverage is strong at the mocked integration level
- we still lack a deterministic end-to-end transport check

### 3. Browser Pagination and Item Extraction E2E

Priority: medium-high

Fold these into the browser system lane with assertions for:

- repeated card extraction via `item_selector`
- multiple page traversal
- expected merged record counts
- stable selector outputs after browser rendering

Why it matters:

- these are high-value user-facing behaviors
- current live Redis automation does not exercise them

### 4. Scheduled Job Real-Queue Automation

Priority: medium

Add one live automation case where a persisted scheduled job triggers through
the real queue path instead of the monkeypatched integration scheduler.

Why it matters:

- scheduled flows currently have strong app-level tests
- the real queue-backed scheduling handoff is still only manually validated

### 5. Restart / Recovery Automation

Priority: medium

Add a system test that submits queued work, restarts Scrapeyard, and verifies
that queued state and final job observability survive restart.

Why it matters:

- durable queueing was a major recent change
- restart resilience is one of the highest-risk operational behaviors

## Non-Goal

Do not automate against Brownells or other external retailer sites. Automated
tests should use local fixture pages so they stay deterministic and do not fail
because a third-party site changed.
