# Epic 3: Run Model Elevation

**Parent spec:** `docs/specs/run-model-and-api-contract.md`
**Spec sections:** 6.1–6.3, 7.1–7.3
**Dependencies:** Epic 1 (schema must exist for storage methods)

---

## Goal

Promote "run" to a first-class persisted model. Create the `JobRun` Pydantic
model, update `Job` and `ErrorRecord` models, and add storage-layer methods
for querying run data.

---

## Tasks

### 3.1 Add `JobRun` model to `models/job.py`

New Pydantic model per spec §6.3:

```python
class JobRun(BaseModel):
    run_id: str
    job_id: str
    status: JobStatus = JobStatus.running
    trigger: str
    config_hash: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    record_count: Optional[int] = None
    error_count: int = 0
```

### 3.2 Update `Job` model in `models/job.py`

Remove fields:
- `last_run_at: Optional[datetime]`
- `run_count: int`

Retain `current_run_id`.

### 3.3 Update `ErrorRecord` model in `models/job.py`

Add field:
- `run_id: str`

### 3.4 Add `get_job_runs()` to `job_store.py`

```python
async def get_job_runs(self, job_id: str, limit: int = 10) -> list[JobRun]:
```

Query: `SELECT * FROM job_runs WHERE job_id = ? ORDER BY started_at DESC LIMIT ?`

### 3.5 Add `get_job_run_stats()` to `job_store.py`

```python
async def get_job_run_stats(self, job_id: str) -> tuple[int, datetime | None]:
```

Returns `(run_count, last_run_at)` derived from `job_runs` table.

### 3.6 Add `list_jobs_with_stats()` to `job_store.py`

```python
async def list_jobs_with_stats(self, project: str | None = None) -> list[tuple[Job, int, datetime | None]]:
```

Uses LEFT JOIN on `job_runs` to derive `run_count` and `last_run_at` per job.
SQL in spec §7.1.

### 3.7 Update `error_store.py`

Include `run_id` in the `log_error` INSERT statement. The `ErrorRecord` model
already carries the field after task 3.3.

### 3.8 Update `result_store.py` return type

Add `ResultPayload` dataclass:

```python
@dataclass(frozen=True, slots=True)
class ResultPayload:
    run_id: str
    data: Any
```

`get_result()` returns `ResultPayload` instead of raw `Any`.

### 3.9 Update `storage/protocols.py`

Update protocol signatures to match the new store method signatures.

---

## Acceptance Criteria

- `JobRun` model can be instantiated and serialized.
- `Job` model no longer has `last_run_at` or `run_count` fields.
- `ErrorRecord` model has `run_id` field.
- `get_job_runs()` returns runs ordered newest-first with limit.
- `get_job_run_stats()` returns correct count and max started_at.
- `list_jobs_with_stats()` returns jobs with derived stats via LEFT JOIN.
- `error_store.log_error()` writes `run_id` to the database.
- `result_store.get_result()` returns `ResultPayload`.
- Protocol interfaces match the new signatures.

---

## Files Touched

| File | Action |
|---|---|
| `src/scrapeyard/models/job.py` | Modify (add JobRun, update Job, update ErrorRecord) |
| `src/scrapeyard/storage/job_store.py` | Modify (add 3 new methods) |
| `src/scrapeyard/storage/error_store.py` | Modify (run_id in INSERT) |
| `src/scrapeyard/storage/result_store.py` | Modify (ResultPayload, get_result return type) |
| `src/scrapeyard/storage/protocols.py` | Modify (protocol signatures) |
