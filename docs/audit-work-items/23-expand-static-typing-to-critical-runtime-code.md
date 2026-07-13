# Expand Static Typing to Critical Runtime Code

> **Status: completed for 0.6.0.** This file is a historical design record;
> its Problem section describes the pre-implementation state. See the
> [archive index](README.md) for completion evidence.

Priority: P2

## Problem

The configured strict mypy lane covers models, config, common, API, and storage, but excludes queue, engine, scheduler, runtime, and webhook modules where most concurrency and lifecycle complexity lives. Several critical paths consequently depend on `Any`, mocks, and runtime conventions rather than checked interfaces.

Relevant files:

- `pyproject.toml`
- `src/scrapeyard/queue/`
- `src/scrapeyard/engine/`
- `src/scrapeyard/scheduler/`
- `src/scrapeyard/runtime/`
- `src/scrapeyard/webhook/`

## Required Outcome

All first-party production modules must pass an intentionally configured mypy lane, with narrow documented boundaries for untyped third-party libraries.

## Implementation Scope

1. Add strict overrides incrementally for queue, webhook, scheduler, runtime, then engine.
2. Replace broad `Any` in internal interfaces with protocols, typed data classes, and explicit callback types.
3. Contain Scrapling, arq, APScheduler, and aiosqlite typing gaps at adapter boundaries.
4. Remove obsolete type ignores and add explanations for unavoidable ignores.
5. Make full-source mypy a required CI check.

## Acceptance Criteria

- `poetry run mypy src/scrapeyard` passes under the agreed configuration.
- New internal APIs do not introduce untyped definitions.
- Third-party `Any` does not flow unchecked through job/run state transitions.
- Existing runtime behavior and public APIs remain unchanged.

## Verification

- Run full-source mypy, Ruff, and pytest.
- Add compile-time protocol examples/tests where adapters are complex.

## Non-Goals

- Do not add runtime validation solely to satisfy the type checker.
