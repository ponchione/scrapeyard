# Epic 3: Run Model Elevation — Execution Plan

## Waves

### Wave 1 (sequential): 01

| Task | File | Action |
|------|------|--------|
| 01-update-models | `src/scrapeyard/models/job.py` | modify |

Add `JobRun` model, trim `Job`, extend `ErrorRecord`. All downstream tasks depend on these model changes.

### Wave 2 (parallel): 02, 03, 04

| Task | File | Action |
|------|------|--------|
| 02-update-job-store | `src/scrapeyard/storage/job_store.py` | modify |
| 03-update-error-store | `src/scrapeyard/storage/error_store.py` | modify |
| 04-update-result-store | `src/scrapeyard/storage/result_store.py` | modify |

Independent storage modules — each consumes updated models but does not touch the others.

### Wave 3 (parallel): 05, 06, 07

| Task | File | Action |
|------|------|--------|
| 05-update-protocols | `src/scrapeyard/storage/protocols.py` | modify |
| 06-fix-test-job-store | `tests/unit/test_job_store.py` | modify |
| 07-fix-test-worker-errors | `tests/unit/test_worker_error_handling.py` | modify |

Protocol alignment and test fixes. Can run in parallel once Wave 2 is complete.
