# Task 02: Add trigger param to dependencies.py

**File:** `src/scrapeyard/api/dependencies.py`
**Action:** modify
**Spec ref:** §5.1

## Change

Update `_task_handler` closure (line 66) to accept `trigger: str = "adhoc"`
kwarg and pass it through to `scrape_task()` as `trigger=trigger`.

## Verify

```bash
poetry run ruff check src/scrapeyard/api/dependencies.py
```
