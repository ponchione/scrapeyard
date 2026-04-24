# Issue 003: Blocking Filesystem Work Inside Async Paths

Severity: High

## Summary

Several async methods perform blocking filesystem operations directly on the event loop, including recursive deletes, JSON serialization, and file reads and writes.

## Evidence

- `src/scrapeyard/storage/result_store.py:64` uses `shutil.rmtree(...)`.
- `src/scrapeyard/storage/result_store.py:69` writes JSON with `path.write_text(json.dumps(..., indent=2))`.
- `src/scrapeyard/storage/result_store.py:127` reads results with `path.read_text()`.
- `src/scrapeyard/storage/result_store.py:140` and `src/scrapeyard/storage/result_store.py:158` recursively delete run directories.
- `src/scrapeyard/storage/cleanup.py:60` also performs `shutil.rmtree(...)` during the background cleanup loop.

## Why It Matters

- Large result payloads can block unrelated requests and workers.
- Recursive deletes can freeze the loop briefly but repeatedly under cleanup or bulk deletion.
- The problem gets worse as result sizes and retention depth increase.

## Recommendation

- Offload filesystem-heavy work to `asyncio.to_thread(...)` or a dedicated I/O executor.
- Avoid pretty-printed JSON in the hot path unless humans read the files directly.
- Consider streaming or chunked serialization if result payloads can become large.

## Deployment Risk

High for any deployment expected to retain sizable result sets or run concurrent jobs.
