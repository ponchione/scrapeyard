# Task 06: Clean test configs

**Files:** `docs/test-configs/*.yaml`, test fixtures
**Action:** modify
**Spec ref:** §3.4

## Change

Search for `format:` under `output:` blocks in test configs and fixtures.
Remove any `output.format` field — it no longer exists on `OutputConfig`.

## Verify

`poetry run pytest tests/unit -x` passes with no OutputConfig validation errors.
