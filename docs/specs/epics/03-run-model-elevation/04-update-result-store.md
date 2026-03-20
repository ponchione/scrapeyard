# Task 04: Update result store with ResultPayload

**File:** `src/scrapeyard/storage/result_store.py`
**Action:** modify
**Spec ref:** §5.4

## Change

Add `ResultPayload` frozen dataclass (fields: run_id, data). Update `get_result()` to return `ResultPayload` — extract run_id from query row, wrap with data in ResultPayload.

## Verify

```bash
python -c "from scrapeyard.storage.result_store import ResultPayload"
```
