# Task 05: Strip format from worker

**File:** `src/scrapeyard/queue/worker.py`
**Action:** modify
**Spec ref:** §3.3

## Change

Delete `_OUTPUT_FORMAT_TO_SAVE` mapping. Remove imports: `OutputFormat` from
config.schema, `get_formatter` from formatters.factory, `format_json` from
formatters.json_fmt, `format_markdown` from formatters.markdown_fmt. Remove
the entire format dispatch block (lines 298-341: `fmt = config.output.format`
through `save_meta = ...`). Replace with temporary direct pass of
`formatted_results` to `result_store.save_result()` without `format` param.

Note: Epic 4 task 4.6 replaces this with proper inline grouping — if done
together, skip the placeholder.

## Verify

`grep -n "formatter\|OutputFormat\|_OUTPUT_FORMAT" src/scrapeyard/queue/worker.py` returns nothing.
