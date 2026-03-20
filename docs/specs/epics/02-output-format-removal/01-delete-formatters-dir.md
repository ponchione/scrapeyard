# Task 01: Delete formatters directory

**File:** `src/scrapeyard/formatters/` (delete entire directory — 5 files)
**Action:** delete
**Spec ref:** §3.3

## Change

Delete all files in the formatters package:
- `__init__.py`
- `factory.py`
- `html_fmt.py`
- `json_fmt.py`
- `markdown_fmt.py`

Note: grouping logic from `json_fmt.py` relocates to the worker in Epic 4 —
spec §5.3 uses simpler `urlparse(tr.url).netloc` keys intentionally.

## Verify

`from scrapeyard.formatters` import fails.
