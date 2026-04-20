# Wide sweep audit findings

Scope checked:
- Top-level architecture/docs: `pyproject.toml`, `README.md`
- Quality baseline: `poetry run ruff check src tests`, `poetry run pytest --collect-only -q`
- Structural hotspot scan across `src/`
- Targeted reads of the main hotspot modules in API, worker, engine, storage, and runtime

Quick overall take:
- The repo is organized and lint-clean, with a strong test surface (`533` tests collected).
- The main maintainability risk is not disorder across the whole repo; it is responsibility concentration in a few orchestration modules plus a handful of repeated helper patterns.
- Most smells are cleanup/refactor candidates, not urgent correctness defects.

## Highest-signal smells

### 1) Orchestration overload in the worker path
Where:
- `src/scrapeyard/queue/worker.py`
- `src/scrapeyard/queue/validation_policy.py`
- `src/scrapeyard/engine/scraper.py`

Evidence:
- `worker.py` is the largest source file at 392 lines and contains the two longest worker-path functions: `scrape_task` (93 lines) and `_fetch_and_validate_target` (74 lines).
- `validation_policy.apply_validation()` is the single longest function in the repo at 122 lines.
- `scraper.py` also carries a long `scrape_target()` path plus several prep/debug helpers.

Why it smells:
- The scrape execution path is spread across multiple modules, but each module still owns several concerns at once: state transitions, retry semantics, validation policy, result shaping, webhook dispatch, and debug artifact handling.
- This creates a change-amplification hotspot: small policy changes are likely to touch multiple orchestration functions.
- The control flow is hard to scan because many side effects happen between the main happy path and several exception/supersession branches.

Suggested direction:
- Keep the existing module split, but push the path further toward explicit step objects or smaller policy functions that each return a narrow typed result.
- In particular, `apply_validation()` and `scrape_task()` look like prime candidates for another round of decomposition.

### 2) Test-patch compatibility aliases are accumulating as quasi-dead surface area
Where:
- `src/scrapeyard/queue/worker.py:377-392`
- `src/scrapeyard/engine/scraper.py:271-285`

Evidence:
- `worker.py` ends with 15 underscore aliases such as `_build_run_paths = build_run_paths` and `_apply_validation = apply_validation`.
- `scraper.py` ends with another alias block for fetch/debug helpers.
- A source search shows these alias names are only defined here in `src/`; they are not part of the production call graph.

Why it smells:
- This is effectively dead production surface kept alive for tests/patch points.
- It increases file length and cognitive noise and makes the public-vs-private boundary harder to understand.
- It is also a maintenance liability: every rename or extraction has to preserve two names.

Suggested direction:
- Prefer testing through the real helper names or move patch surfaces behind explicit seams/modules instead of alias-exporting internals at file bottom.

### 3) API route layer has repetitive manual response/error shaping
Where:
- `src/scrapeyard/api/routes.py`
- `src/scrapeyard/api/response_utils.py`
- `src/scrapeyard/api/serializers.py`
- `src/scrapeyard/api/query_parsing.py`

Evidence:
- `routes.py` is 313 lines and repeatedly does `try/except` -> `json_error(...)`, explicit pagination boilerplate, and manual `json_response(...)` payload wrapping.
- The same 404/400 patterns appear several times for jobs/results/errors.
- Response payload assembly is split between serializers and route-local wrappers, so the layer is only partially normalized.

Why it smells:
- This is a low-level duplication smell rather than a big architectural flaw.
- Repetition makes route additions noisier and easier to make slightly inconsistent.
- The response helpers are so thin that they do not reduce much complexity; they mostly preserve a manual style everywhere.

Suggested direction:
- Consider centralizing common route error handling and pagination response construction.
- A small set of typed response models or route helper decorators would reduce repeated `try/except`/`json_error` code.

### 4) Selector code duplicates strict and non-strict execution paths
Where:
- `src/scrapeyard/engine/selectors.py`

Evidence:
- `extract_selectors()` and `extract_selectors_strict()` share almost the entire extraction loop.
- `select_items()`/`select_items_strict()` and `count_selector_matches()`/`count_selector_matches_strict()` follow the same wrapper pattern.

Why it smells:
- This is classic maintenance duplication: if extraction semantics change, both strict and forgiving paths must stay in lockstep.
- The duplication is small but structural, so it is easy for one path to drift.

Suggested direction:
- Funnel both modes through one internal implementation with a strictness flag or exception policy callback.

### 5) Detection helpers are useful but somewhat over-commented and a little repetitive
Where:
- `src/scrapeyard/engine/detection.py`

Evidence:
- The module is branch-heavy (51 branch nodes, highest in the repo).
- It contains several large section banners and step-by-step comments that restate nearby code.
- `_normalize_price_text()` and `_normalize_stock_signal_text()` are nearly the same shape.
- The pricing flow separately walks text patterns, CSS selectors, and price-value patterns with a lot of procedural state (`matched`, `display_text`).

Why it smells:
- The module is understandable, but it is verbose in a way that increases scanning cost.
- Some comments explain the exact sequence already obvious from the code; that is the kind of comment that tends to go stale.
- Small helper duplication suggests the normalization/modeling layer is not fully factored yet.

Suggested direction:
- Trim explanatory comments that merely narrate the next line.
- Consider pulling the repeated normalization logic behind one text-normalization helper and making pattern phases more data-driven.

### 6) Storage layer has several “copy-edit this in three places” patterns
Where:
- `src/scrapeyard/storage/job_store.py`
- `src/scrapeyard/storage/result_store.py`

Evidence:
- `job_store.py` has many near-similar CRUD/update methods with repeated `get_db(...)`, SQL execution, commit, and rowcount checking.
- Duplicate detection in `save_job()` relies on matching `IntegrityError` text, which is brittle and hard to port.
- `result_store.py` has one deletion path in `_delete_by_ids()` and another in `delete_results()` that partly re-implements similar behavior.

Why it smells:
- This is not unusual for a SQLite-backed service, but the repetition raises the cost of storage behavior changes.
- String-matching database errors is especially non-ergonomic and easy to miss during schema/index changes.

Suggested direction:
- Consolidate repeated write/update patterns further, especially around delete/update helpers.
- If possible, isolate uniqueness handling into a narrower helper instead of string-parsing SQLite errors inline.

## Lower-priority smells

### 7) Unused-looking runtime state assignment in `main.py`
Where:
- `src/scrapeyard/main.py`

Evidence:
- `app.state.job_store`, `error_store`, `result_store`, and `webhook_dispatcher` are assigned during startup.
- A source search only found `app.state.*` reads for `cleanup_task`, `scheduler`, and `worker_pool` in `main.py` itself.

Why it smells:
- Some app-state assignment appears to be ceremonial rather than actually consumed.
- That makes startup wiring look more dynamic than it really is.

Suggested direction:
- Either use app state consistently for dependency access or trim the unused state fields.

### 8) Some comments are more historical than helpful
Where:
- `src/scrapeyard/storage/database.py`
- `src/scrapeyard/engine/scraper.py`
- `src/scrapeyard/webhook/dispatcher.py`
- `src/scrapeyard/common/settings.py`

Examples:
- Large separator banners in `database.py` and `detection.py`
- “Backwards-compatible aliases for tests and patch surfaces” comments attached to alias blocks
- Settings category comments that mostly restate the field grouping

Why it smells:
- None of these are harmful alone, but together they add to visual noise.
- The repo generally reads cleanly enough that some of these comments are not earning their keep.

## Healthy signals worth keeping

- Clear package boundaries: API, queue, engine, storage, scheduler, runtime, webhook.
- Lint baseline is clean: `ruff` passes on `src` and `tests`.
- Strong automated test footprint: `533` tests collected.
- Good use of small supporting modules in several areas (`job_queries`, `job_rows`, `result_queries`, `response_utils`, etc.).
- The codebase already shows some active decomposition work; the main issue is that the heaviest paths still need another pass.

## Priority order if cleanup is desired

1. Worker path decomposition (`worker.py`, `validation_policy.py`, `scraper.py`)
2. Remove or redesign alias-based test seams
3. Normalize API response/error handling patterns
4. Deduplicate selector strict/non-strict logic
5. Trim verbose comments and small helper duplication in detection/storage

## Short verdict

This codebase does not look messy; it looks like a maturing service with a few high-traffic modules carrying too much orchestration weight. The main cleanup opportunity is to reduce hotspot complexity and repeated policy plumbing, not to do a repo-wide rewrite.
