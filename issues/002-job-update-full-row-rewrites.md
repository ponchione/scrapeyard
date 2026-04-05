# Issue 002: Full-Row Job Rewrites For Small State Changes

Severity: High

## Summary

Job status transitions rewrite the entire `jobs` row, including large immutable fields like `config_yaml`, even when only status metadata changed.

## Evidence

- `src/scrapeyard/storage/job_store.py:77` updates `project`, `name`, `status`, `config_yaml`, `created_at`, `updated_at`, schedule fields, and `current_run_id` every time.
- `src/scrapeyard/queue/worker.py:66` and `src/scrapeyard/queue/worker.py:423` use that broad update path for routine lifecycle transitions.

## Why It Matters

- Large YAML configs are rewritten repeatedly during normal execution.
- Extra write volume increases SQLite journal work and lock time.
- This cost is pure overhead and compounds as job counts increase.

## Recommendation

- Split job persistence into narrower update methods such as:
  - `update_job_status(...)`
  - `update_job_schedule_state(...)`
  - `update_job_definition(...)`
- Keep immutable or rarely changed columns out of status-only updates.

## Deployment Risk

Medium to high. It is avoidable hot-path I/O on every run.
