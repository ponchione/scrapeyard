# Epic 4: Worker Run Lifecycle

**Parent spec:** `docs/specs/run-model-and-api-contract.md`
**Spec sections:** 5.1–5.4
**Dependencies:** Epic 1 (schema), Epic 2 (format removal), Epic 3 (models/storage)

---

## Goal

Wire the `trigger` parameter through the enqueue path, implement the full
run lifecycle (create → track → finalize) in `scrape_task`, and replace the
formatter dispatch with inline JSON grouping logic.

---

## Tasks

### 4.1 Add `trigger` parameter to enqueue path

Thread `trigger: str = "adhoc"` through the full call chain:

- `WorkerPool.enqueue()` — new kwarg
- `WorkerPool._run_job()` — new kwarg
- `WorkerPool._execute()` — new kwarg
- `scrape_task()` — new kwarg

Signatures specified in spec §5.1.

### 4.2 Insert `job_runs` row at run start

After config parse and `_should_skip_delivery` check, before target processing:

1. Compute `config_hash = hashlib.sha256(config_yaml.encode()).hexdigest()`.
2. INSERT into `job_runs` with status `running`.

Code in spec §5.2 Step 1.

### 4.3 Pass `run_id` to error logging

Every call to `_log_error()` in the worker must include `run_id`. The
`ErrorRecord` model already has the field (Epic 3). Ensure the worker
constructs `ErrorRecord` with `run_id` populated.

### 4.4 Finalize run row at completion

After result save, before webhook dispatch:

1. Count errors: `SELECT COUNT(*) FROM errors WHERE run_id = ?`.
2. UPDATE `job_runs` with `status`, `completed_at`, `record_count`, `error_count`.
3. Remove `run_count` increment and `last_run_at` assignment from job row update.

Code in spec §5.2 Steps 3–4.

### 4.5 Finalize run row on crash

In the `except Exception` crash handler:

1. UPDATE `job_runs` SET `status = 'failed'`, `completed_at = now()`
   WHERE `run_id = ? AND status = 'running'`.
2. The `AND status = 'running'` guard prevents overwriting a run that
   completed before the crash (e.g., crash during webhook dispatch).

Code in spec §5.2 Step 5.

### 4.6 Inline JSON grouping logic

Replace `get_formatter(fmt)` / `formatter(...)` with inline grouping:

- **`group_by == GroupBy.merge`:** Flat list with `_source` field injected.
- **`group_by == GroupBy.target` (default):** Dict keyed by domain with
  `status`, `count`, `data`.

Grouping logic sourced from `formatters/json_fmt.py`. Code in spec §5.3.

### 4.7 Update `save_result` call

Call `result_store.save_result()` with simplified signature (no `format`,
no `file_contents`). Pass `run_id`, `status`, `record_count`.

Code in spec §5.3.

---

## Acceptance Criteria

- `trigger` propagates from `enqueue()` through to `scrape_task()`.
- A `job_runs` row is created at run start with status `running`.
- Errors logged during the run carry the correct `run_id`.
- On successful completion, `job_runs` row has correct `status`,
  `completed_at`, `record_count`, `error_count`.
- On crash, `job_runs` row is finalized with `status = 'failed'`.
- JSON grouping produces identical output to the old formatter system.
- No references to the formatter system remain in the worker.

---

## Files Touched

| File | Action |
|---|---|
| `src/scrapeyard/queue/pool.py` | Modify (trigger param on enqueue, _run_job, _execute) |
| `src/scrapeyard/queue/worker.py` | Modify (trigger param, run lifecycle, inline grouping, error run_id) |
