# Stabilize API Response Contracts

> **Status: completed for 0.6.0.** This file is a historical design record;
> its Problem section describes the pre-implementation state. See the
> [archive index](README.md) for completion evidence.

Priority: P2

## Problem

Most routes disable response models, generated OpenAPI schemas are weak, validation errors have more than one shape, and persisted result documents are wrapped again by API serializers. Sync and result responses therefore contain redundant nested job/status/result metadata and are harder for clients to model safely.

Relevant code:

- `src/scrapeyard/api/routes.py`
- `src/scrapeyard/api/serializers.py`
- `src/scrapeyard/api/response_utils.py`
- `src/scrapeyard/queue/worker.py`
- `src/scrapeyard/storage/types.py`

## Required Outcome

Every endpoint must have a typed, documented, consistent response and error contract without redundant result nesting.

## Implementation Scope

1. Define Pydantic response models for jobs, runs, results, errors, health, queued submissions, and terminal sync submissions.
2. Define one error envelope for custom HTTP errors, request validation, authentication, size limits, and rate limits.
3. Separate stored artifact content from API envelope metadata, or explicitly version the current nested format.
4. Correct timed-out sync responses so their status reflects current persisted state rather than always saying `queued`.
5. Decide and document pagination metadata placement; migrate headers only if compatibility requires it.
6. Add an API versioning or compatibility policy before changing existing shapes.

## Acceptance Criteria

- OpenAPI contains concrete success and error schemas for every route.
- Equivalent validation failures return the same error envelope.
- Result records have one unambiguous location in sync and polling responses.
- Existing Eyebox consumers have a migration path or compatibility layer.
- Serializers do not expose secrets or local-only details unintentionally.

## Verification

- Add response-model and OpenAPI snapshot/contract tests.
- Add integration tests for every error source and result grouping mode.
- Run the full suite and validate a representative Eyebox client against the new contract.

## Non-Goals

- Do not add unrelated endpoints in this task.
