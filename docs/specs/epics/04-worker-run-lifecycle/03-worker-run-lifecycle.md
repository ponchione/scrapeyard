# Task 03: Worker run lifecycle changes

**File:** `src/scrapeyard/queue/worker.py`
**Action:** modify
**Spec ref:** §5.1, §5.2, §5.3

## Change

All worker.py changes in sequence (single file — cannot parallelize):

1. **Signature**: Add `trigger: str = "adhoc"` kwarg to `scrape_task()`.
   Add `import hashlib` and `from scrapeyard.storage.database import get_db`.

2. **Run insert**: After job status set to running (line 75), INSERT into
   `job_runs` with `status='running'` (guarded by `if run_id is not None`).
   Compute `config_hash = hashlib.sha256(config_yaml.encode()).hexdigest()`.

3. **Error run_id**: Add `run_id: str | None = None` param to `_log_error()`.
   Add `run_id=run_id or ""` to ErrorRecord construction. Add `run_id=run_id`
   to all 5 call sites.

4. **Inline grouping**: Replace format dispatch block (lines 291-341) with
   inline JSON grouping using `config.output.group_by` / `GroupBy.merge` vs
   `GroupBy.target` per spec §5.3. Remove `formatted_results` list
   comprehension. Update imports: remove `OutputFormat`, add `GroupBy`. Remove
   formatter imports.

5. **Run finalize**: After result save, count errors via
   `SELECT COUNT(*) FROM errors WHERE run_id = ?`, UPDATE `job_runs` with
   `status`/`completed_at`/`record_count`/`error_count`.

6. **Remove denorm**: Remove `"last_run_at": completed_at` and
   `"run_count": job.run_count + 1` from final job update `model_copy`.

7. **Crash handler**: In except block, after marking job failed, UPDATE
   `job_runs SET status='failed', completed_at=now WHERE run_id=? AND
   status='running'`.

## Verify

```bash
poetry run ruff check src/scrapeyard/queue/worker.py
```
