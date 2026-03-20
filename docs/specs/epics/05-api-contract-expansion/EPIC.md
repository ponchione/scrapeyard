# Epic 5: API Contract Expansion

**Parent spec:** `docs/specs/run-model-and-api-contract.md`
**Spec sections:** 4.1–4.4, 8.1–8.5
**Dependencies:** Epic 3 (storage methods), Epic 4 (worker trigger), Epic 6 (scheduler method)

---

## Goal

Update all API route handlers to serve the new contract: expanded job detail
with run history, derived run stats on the list endpoint, `run_id` on results
and errors, and `trigger` on the scrape endpoint.

---

## Tasks

### 5.1 Expand `GET /jobs/{job_id}` handler

Add dependencies:
- `SchedulerService` (via `get_scheduler`).

Build expanded response with:
- `config_yaml` — from job row.
- `next_run_at` — from `scheduler.get_next_run_time(job_id)`.
- `run_count` — from `job_store.get_job_run_stats(job_id)`.
- `last_run_at` — from `job_store.get_job_run_stats(job_id)`.
- `runs` — from `job_store.get_job_runs(job_id, limit=10)`.

Response shape in spec §4.1.

### 5.2 Update `GET /jobs` handler

Replace `job_store.list_jobs(project)` with
`job_store.list_jobs_with_stats(project)`.

Build response dicts with derived `run_count` and `last_run_at`.

List response does NOT include `config_yaml`, `next_run_at`, or `runs`.

Response shape in spec §4.2.

### 5.3 Update `GET /results/{job_id}` handler

- `result_store.get_result()` now returns `ResultPayload`.
- Include `run_id` in response envelope alongside `job_id`, `status`, `results`.

Response shape in spec §4.3.

### 5.4 Update `GET /errors` handler

Include `run_id` in each error response object. The field is now available
from the `ErrorRecord` model and stored in the database.

### 5.5 Update `POST /scrape` handler

Pass `trigger="adhoc"` to `worker_pool.enqueue()`.

---

## Acceptance Criteria

- `GET /jobs/{job_id}` returns `config_yaml`, `next_run_at`, `run_count`,
  `last_run_at`, and `runs` array (last 10, newest first).
- `GET /jobs` returns `run_count` and `last_run_at` per job (derived, not
  from job row columns).
- `GET /results/{job_id}` includes `run_id` in the response envelope.
- `GET /errors` includes `run_id` on each error record.
- `POST /scrape` passes `trigger="adhoc"` through to the worker pool.
- Error responses (202, 400, 404) unchanged.

---

## Files Touched

| File | Action |
|---|---|
| `src/scrapeyard/api/routes.py` | Modify (all five handlers) |
| `src/scrapeyard/api/dependencies.py` | Possibly modify (if scheduler DI needs wiring) |
