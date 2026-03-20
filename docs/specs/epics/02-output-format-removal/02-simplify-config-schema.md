# Task 02: Simplify config schema

**File:** `src/scrapeyard/config/schema.py`
**Action:** modify
**Spec ref:** §3.2, §3.4

## Change

Delete the `OutputFormat` enum entirely (lines 59-64). Remove the `format`
field from `OutputConfig` — only `group_by` remains. Update docstring to
"Output grouping settings."

## Verify

`grep -r OutputFormat src/` returns nothing.
