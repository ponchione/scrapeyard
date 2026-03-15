# Fix POST /scrape UNIQUE Constraint Collision

**Date:** 2026-03-15
**Work Order:** 000-fix-adhoc-job-unique-constraint

## Problem

Submitting the same YAML config to `POST /scrape` more than once causes a 500 IntegrityError. The `jobs` table enforces `UNIQUE(project, name)`, and ad-hoc scrapes use `config.name` verbatim, so re-runs collide.

Scheduled jobs (`POST /jobs`) intentionally rely on this uniqueness and must not be changed.

## Solution

Append a short UUID suffix to the job name in the `POST /scrape` route handler only.

```python
# Before
name=config.name

# After
name=f"{config.name}-{uuid.uuid4().hex[:8]}"
```

This produces names like `my-scrape-a3b7c9d2` — human-readable with negligible collision probability (8 hex chars = ~4 billion values).

## Files Changed

| File | Change |
|------|--------|
| `src/scrapeyard/api/routes.py` | Modify line 84 in `scrape()`: append `-{short_uuid}` to `config.name` for ad-hoc jobs |
| `tests/integration/test_scrape_lifecycle.py` | Add `test_duplicate_adhoc_scrape_does_not_collide` |

## Files NOT Changed

- `sql/001_create_jobs.sql` — `UNIQUE(project, name)` constraint preserved
- `src/scrapeyard/models/job.py` — Job model unchanged
- `src/scrapeyard/storage/job_store.py` — store logic unchanged
- `POST /jobs` route — scheduled job creation unchanged

## New Test

`test_duplicate_adhoc_scrape_does_not_collide`:
1. Submit the same ad-hoc YAML config twice via `POST /scrape`
2. Assert both requests return 200 or 202 (no 500)
3. Assert the two responses have different `job_id` values

## Design Decisions

- **Short UUID over timestamp:** `uuid4` is already imported in `routes.py`. Timestamps risk same-second collisions. The `created_at` field already records timing.
- **Suffix in route handler, not job store:** Per work order constraint. Keeps the store generic and the ad-hoc vs. scheduled distinction explicit at the API layer.
- **8 hex characters:** Balances readability and collision resistance. Shorter suffixes (4 chars) have meaningful collision probability at scale; longer ones reduce readability for no practical benefit.

## Acceptance Criteria (from work order)

- [x] `POST /scrape` with same project+name does not return 500
- [x] Ad-hoc jobs auto-generate a unique, human-readable suffix
- [x] Scheduled jobs via `POST /jobs` are not affected
- [x] Existing tests continue to pass
- [x] New integration test submits same config twice and both succeed
