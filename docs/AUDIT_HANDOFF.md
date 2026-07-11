# Audit Implementation Handoff

Last updated: 2026-07-10

## Where we left off

Audit items 01 through 09 are implemented cumulatively in the current working
tree. Item 09, result-artifact reconciliation, is complete. No work has begun
on item 10 or any later item.

The working tree is authoritative and intentionally uncommitted. Preserve all
existing tracked and untracked changes, including the Docker PID-1/setpriv
correction and `src/scrapeyard/queue/priority.py` from item 08. Do not reset,
restore, checkout, stash, clean, broadly reformat, stage, commit, or push unless
the user explicitly changes those instructions.

`HEAD`, `main`, and `origin/main` all remain at:

```text
e9c6cfe0a5977bbcca46d0b7bcaa2e2c94166ae6
```

Immediately before this handoff file was added, the cumulative tracked diff
contained 82 modified files, 11,248 insertions, and 1,370 deletions. There were
48 pre-existing untracked files and an empty index. This handoff is one new
untracked file, so `git ls-files --others --exclude-standard` should now report
49 files. Nothing was staged, committed, pushed, reset, restored, stashed,
cleaned, or discarded.

## Item 09 implementation

The main implementation is in:

- `src/scrapeyard/storage/result_store.py`
- `src/scrapeyard/storage/filesystem.py`
- `src/scrapeyard/storage/cleanup.py`
- `src/scrapeyard/storage/types.py`
- `src/scrapeyard/storage/protocols.py`
- `src/scrapeyard/storage/job_store.py`
- `src/scrapeyard/api/dependencies.py`
- `src/scrapeyard/common/settings.py`

`results_meta.file_path` is authoritative when it identifies exactly one
contained `project/job/run` directory. A valid artifact has a regular,
non-symlink `results.json` containing valid UTF-8 JSON. Metadata remains
authoritative after parent deletion with `delete_results=false`.

Reconciliation:

- scans only beneath `storage_results_dir` at exact project/job/run depth;
- uses non-following stats, `O_NOFOLLOW` where supported, and symlink-safe
  recursive removal;
- reports but does not repair or delete metadata for missing, corrupt,
  unreadable, unsafe, symlinked, or non-regular result artifacts;
- protects recent work with a 24-hour default grace period;
- protects exact queued/running project, job-name, and `current_run_id`
  ownership from `jobs.db`, with metadata/active/timestamp rechecks before
  destructive removal;
- removes only exact known atomic temp names for `results.json`,
  `dynamic-main.png`, and `stealthy-main.png` with a positive PID and
  32-lowercase-hex UUID;
- supports typed dry-run and destructive reports and converges idempotently;
- keeps blocking traversal, parsing, accounting, and removal off the event
  loop through cancellation-safe thread offloading.

Periodic cleanup order is expired-result retention, per-job pruning,
orphan/temp reconciliation, then webhook tombstone scrubbing. Normal retention
remains metadata-first; explicit deletion remains filesystem-first and
retryable. Reconciliation failures are isolated and retried on the next pass,
while cleanup-task cancellation still propagates.

New settings are:

```text
SCRAPEYARD_STORAGE_ORPHAN_GRACE_SECONDS=86400
SCRAPEYARD_STORAGE_RECONCILIATION_DRY_RUN=true
```

They are wired through Pydantic settings, Compose, cleanup, tests, README, and
deployment documentation.

## Verification checkpoint

Focused verification passed:

- `tests/unit/test_filesystem.py`: 11 passed
- `tests/unit/test_result_store.py`: 26 passed
- `tests/unit/test_result_store_cleanup.py`: 25 passed
- `tests/unit/test_cleanup.py`: 6 passed
- `tests/unit/test_cleanup_loop.py`: 4 passed
- `tests/unit/test_job_deletion.py`: 11 passed
- `tests/integration`: 59 passed

Final verification passed:

```text
poetry run ruff check src tests
  All checks passed.

poetry run mypy src
  Success: no issues found in 77 source files.

poetry run pytest
  1,099 passed, 8 skipped, 89.16% coverage.

git diff --check
  Passed with no output.

poetry run pytest --no-cov tests/live_redis -rs
  8 passed, with 9 known arq close() deprecation warnings.
```

The isolated `scrapeyard-live-redis` container was stopped and auto-removed.
No container with that name exists, and `127.0.0.1:56379` is free.

## Known limits

Filesystem and SQLite changes are not one transaction. Filesystem timestamps
can be coarse or administratively changed, permissions can make candidates
unverifiable, and a final active-state/metadata race remains after the last
check. Symlink-safe operations reduce but cannot eliminate hostile concurrent
path replacement. There is no multi-instance reconciliation lease. Corrupt
content is diagnosed only; no recovery, metadata synthesis, or repair is
attempted.

## Where to pick up

At the start of the next session, treat the working tree as authoritative and
run the preservation checks before editing:

```bash
git status --short --branch
git rev-parse HEAD main origin/main
git diff --stat
git diff --cached --stat
git ls-files --others --exclude-standard
docker ps -a --filter name='^/scrapeyard-live-redis$'
ss -ltn '( sport = :56379 )'
```

Confirm the index is empty, all three refs still match the commit above, this
handoff is the only additional untracked file beyond the prior 48, the Redis
container is absent, and port 56379 is free. Then read `AGENTS.md` and the audit
work-item document explicitly assigned by the user. If the next assignment is
item 10, build directly on items 01 through 09; do not restart or redesign any
completed implementation.
