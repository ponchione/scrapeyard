# Add Scheduled-Job Management and Explicit Timezones

> **Status: completed for 0.6.0.** This file is a historical design record;
> its Problem section describes the pre-implementation state. See the
> [archive index](README.md) for completion evidence.

Priority: P2

## Problem

Scheduled jobs can be created, read, listed, and deleted, but not updated, paused, resumed, or manually triggered. A job created with `schedule.enabled: false` cannot be enabled through HTTP. Cron interpretation also relies on an implicit runtime timezone.

Relevant code:

- `src/scrapeyard/api/routes.py`
- `src/scrapeyard/scheduler/cron.py`
- `src/scrapeyard/storage/job_store.py`
- `src/scrapeyard/config/schema.py`

## Required Outcome

Operators must be able to manage a scheduled job throughout its lifecycle, with stable and visible timezone semantics.

## Implementation Scope

1. Add an explicit timezone field with a documented default and IANA timezone validation.
2. Persist timezone alongside cron and include it in API responses.
3. Add endpoints to update schedule/config, pause, resume, and trigger a run now.
4. Define update behavior while a run is queued or active; new config should apply to a clearly identified future run.
5. Keep unique project/name behavior and config hashes intact.
6. Make scheduler registration changes consistent with database state if either operation fails.
7. Define missed-run/coalescing behavior across downtime and clock changes.

## Acceptance Criteria

- A disabled schedule can be enabled without deleting the job.
- Pause/resume survives process restart.
- A manual trigger produces a run with an explicit trigger value and uses the documented config version.
- Cron fires at the expected time in non-UTC zones and across daylight-saving transitions.
- Failed scheduler updates do not leave silently divergent database state.

## Verification

- Add timezone and DST unit tests using frozen time.
- Add API integration tests for update, pause, resume, manual trigger, active-run conflict, and restart rehydration.
- Run scheduler, storage, API, live-Redis, and full suites.

## Non-Goals

- Calendar-style schedules beyond cron are not required.
