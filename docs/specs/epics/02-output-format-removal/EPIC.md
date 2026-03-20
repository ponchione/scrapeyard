# Epic 2: Output Format Removal

**Parent spec:** `docs/specs/run-model-and-api-contract.md`
**Spec sections:** 3.1–3.5
**Dependencies:** Epic 1 (schema changes for `results_meta` format column removal)

---

## Goal

Strip the output format system down to JSON-only. Delete the `formatters/`
module, remove the `OutputFormat` enum, simplify `OutputConfig`, and clean up
all references in the worker, result store, and config layer.

---

## Tasks

### 2.1 Delete `src/scrapeyard/formatters/` directory

Remove all five files: `__init__.py`, `factory.py`, `html_fmt.py`,
`json_fmt.py`, `markdown_fmt.py`.

**Before deleting**, extract the grouping logic from `json_fmt.py`
(`format_json` function) — it will be needed in Epic 4 (worker inline
assembly). Save the logic as a reference or move it directly if Epics 2 and 4
are done together.

### 2.2 Remove `OutputFormat` enum from `config/schema.py`

Delete the enum entirely. No consumer remains.

### 2.3 Simplify `OutputConfig` in `config/schema.py`

Replace the current `OutputConfig` model with:

```python
class OutputConfig(BaseModel):
    """Output grouping settings."""
    group_by: GroupBy = Field(default=GroupBy.target, description="Result grouping strategy")
```

Remove any `format` field. The `GroupBy` enum (`target`, `merge`) is unchanged.

### 2.4 Remove format-related code from `storage/result_store.py`

- Delete `_FORMAT_FILES` mapping.
- Remove `format` parameter from `save_result()`.
- Remove `file_contents` parameter from `save_result()`.
- Simplify `get_result()` to always read `results.json`.
- Remove `format` from the `results_meta` INSERT statement.

### 2.5 Remove format-related code from `queue/worker.py`

- Delete `_OUTPUT_FORMAT_TO_SAVE` mapping.
- Remove formatter imports and dispatch (`get_formatter`, `formatter(...)` call).
- The inline JSON grouping replacement is covered in Epic 4 — this task only
  removes the old code.

### 2.6 Delete formatter tests

Remove any unit tests that cover the `formatters/` module.

---

## Acceptance Criteria

- `src/scrapeyard/formatters/` directory no longer exists.
- `OutputFormat` enum no longer exists anywhere in the codebase.
- `OutputConfig` contains only `group_by`.
- No imports of `formatters` remain in any module.
- `ruff check` passes.
- `result_store.save_result()` has no `format` parameter.

---

## Files Touched

| File | Action |
|---|---|
| `src/scrapeyard/formatters/__init__.py` | Delete |
| `src/scrapeyard/formatters/factory.py` | Delete |
| `src/scrapeyard/formatters/html_fmt.py` | Delete |
| `src/scrapeyard/formatters/json_fmt.py` | Delete |
| `src/scrapeyard/formatters/markdown_fmt.py` | Delete |
| `src/scrapeyard/config/schema.py` | Modify (remove OutputFormat, simplify OutputConfig) |
| `src/scrapeyard/storage/result_store.py` | Modify (remove format param, mappings) |
| `src/scrapeyard/queue/worker.py` | Modify (remove formatter imports/dispatch) |
| `tests/unit/test_formatters*.py` (if exists) | Delete |
