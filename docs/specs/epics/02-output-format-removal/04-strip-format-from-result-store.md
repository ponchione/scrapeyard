# Task 04: Strip format from result store

**File:** `src/scrapeyard/storage/result_store.py`
**Action:** modify
**Spec ref:** §5.4

## Change

Delete `_FORMAT_FILES` mapping. Remove `format` and `file_contents` params
from `save_result()`. Remove format validation. Replace multi-format file loop
with single `results.json` write. Remove `format` from results_meta INSERT.
In `get_result()`, remove `format` from SELECT and always read `results.json`.
Return type stays `Any` for now (ResultPayload comes in Epic 3).

## Verify

`grep -n "format" src/scrapeyard/storage/result_store.py` returns no format-related hits.
