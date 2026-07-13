# Add Idempotent Scrape Submission

> **Status: completed for 0.6.0.** This file is a historical design record;
> its Problem section describes the pre-implementation state. See the
> [archive index](README.md) for completion evidence.

Priority: P1

## Problem

Every retry of `POST /scrape` creates a new ad-hoc job name, job ID, and run ID. A client retry after a network timeout can therefore duplicate expensive browser work and produce multiple result/webhook streams.

Relevant code:

- `src/scrapeyard/api/routes.py`
- `src/scrapeyard/api/scrape_submission.py`
- `src/scrapeyard/storage/job_store.py`
- `sql/001_create_jobs.sql`

## Required Outcome

Clients must be able to retry submission safely with a caller-provided idempotency key and receive the original submission outcome.

## Implementation Scope

1. Accept a bounded `Idempotency-Key` header on `POST /scrape`.
2. Scope keys by authenticated caller identity or API-key digest and define behavior when auth is disabled.
3. Persist the key, request/config hash, job ID, run ID, response mode, and creation time atomically with job creation.
4. Return the existing job for a repeated matching request.
5. Return 409 for the same key with different request content.
6. Handle concurrent duplicate submissions without duplicate enqueue.
7. Define retention and cleanup for idempotency records.

## Acceptance Criteria

- Concurrent identical requests with one key enqueue exactly one run.
- Repeated identical requests return the same job/run and a documented response status.
- Reusing a key with different YAML is rejected.
- Behavior remains unchanged when the header is omitted.
- Keys and API credentials are never logged in plaintext.

## Verification

- Add store race tests and API integration tests for matching, conflicting, expired, and missing keys.
- Add a live-Redis concurrency test.
- Run the full suite.

## Non-Goals

- Scheduled-job name uniqueness remains a separate contract.
