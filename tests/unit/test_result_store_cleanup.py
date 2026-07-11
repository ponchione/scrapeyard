"""Test result retention cleanup."""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, call, patch

import pytest

import scrapeyard.storage.result_store as result_store_module
from scrapeyard.storage.database import get_db, init_db, reset_db
from scrapeyard.storage.result_store import LocalResultStore


@pytest.fixture
async def store(tmp_path):
    db_dir = tmp_path / "db"
    results_dir = tmp_path / "results"
    await init_db(str(db_dir))

    async def _lookup(job_id: str) -> tuple[str, str]:
        return ("test-project", "test-job")

    store = LocalResultStore(str(results_dir), _lookup)
    yield store
    reset_db()


async def _run_to_thread(func, *args, **kwargs):
    return func(*args, **kwargs)


NOW = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)


async def _lookup(_job_id: str) -> tuple[str, str]:
    return ("test-project", "test-job")


def _backdate_tree(path: Path, *, days: int = 2) -> None:
    timestamp = (NOW - timedelta(days=days)).timestamp()
    for root, directories, files in os.walk(path, topdown=False, followlinks=False):
        for name in files:
            os.utime(Path(root) / name, (timestamp, timestamp), follow_symlinks=False)
        for name in directories:
            os.utime(Path(root) / name, (timestamp, timestamp), follow_symlinks=False)
    os.utime(path, (timestamp, timestamp), follow_symlinks=False)


async def _insert_metadata(
    *,
    job_id: str,
    run_id: str,
    file_path: Path,
) -> None:
    from scrapeyard.storage.database import get_db

    async with get_db("results_meta.db") as db:
        await db.execute(
            """INSERT INTO results_meta
               (job_id, project, run_id, status, record_count, file_path, created_at)
               VALUES (?, 'test-project', ?, 'complete', 1, ?, ?)""",
            (job_id, run_id, str(file_path), NOW.isoformat()),
        )
        await db.commit()


@pytest.mark.asyncio
async def test_delete_expired_removes_old_results(store, tmp_path):
    # Save a result.
    meta = await store.save_result("job-1", [{"url": "http://example.com"}])
    run_id = meta.run_id

    # Manually backdate the created_at to 31 days ago.
    from scrapeyard.storage.database import get_db

    old_date = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    async with get_db("results_meta.db") as db:
        await db.execute(
            "UPDATE results_meta SET created_at = ? WHERE run_id = ?",
            (old_date, run_id),
        )
        await db.commit()

    deleted = await store.delete_expired(30)
    assert deleted >= 1

    # Verify the result is gone from DB.
    async with get_db("results_meta.db") as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM results_meta WHERE run_id = ?", (run_id,)
        )
        row = await cursor.fetchone()
    assert row[0] == 0


@pytest.mark.asyncio
async def test_delete_expired_keeps_fresh_results(store):
    meta = await store.save_result("job-2", [{"url": "http://example.com"}])
    run_id = meta.run_id
    deleted = await store.delete_expired(30)
    assert deleted == 0

    # Verify the result is still in DB.
    from scrapeyard.storage.database import get_db

    async with get_db("results_meta.db") as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM results_meta WHERE run_id = ?", (run_id,)
        )
        row = await cursor.fetchone()
    assert row[0] == 1


@pytest.mark.asyncio
async def test_delete_expired_offloads_directory_removal(store):
    meta = await store.save_result("job-3", [{"url": "http://example.com"}])
    run_id = meta.run_id

    from scrapeyard.storage.database import get_db

    old_date = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    async with get_db("results_meta.db") as db:
        await db.execute(
            "UPDATE results_meta SET created_at = ? WHERE run_id = ?",
            (old_date, run_id),
        )
        await db.commit()

    with patch.object(
        result_store_module.asyncio,
        "to_thread",
        new_callable=AsyncMock,
    ) as mock_to_thread:
        mock_to_thread.side_effect = _run_to_thread
        deleted = await store.delete_expired(30)

    assert deleted == 1
    assert mock_to_thread.await_args == call(
        result_store_module.remove_directories,
        [Path(meta.file_path)],
    )


@pytest.mark.asyncio
async def test_delete_expired_chunks_metadata_ids(store, monkeypatch):
    run_ids: list[str] = []
    for index in range(5):
        meta = await store.save_result(
            f"job-chunk-{index}",
            {"index": index},
            run_id=f"run-chunk-{index}",
        )
        run_ids.append(meta.run_id)

    old_date = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    async with get_db("results_meta.db") as db:
        await db.execute("UPDATE results_meta SET created_at = ?", (old_date,))
        await db.commit()

    monkeypatch.setattr(result_store_module, "_DELETE_ID_BATCH_SIZE", 2)
    statements: list[str] = []
    async with get_db("results_meta.db") as db:
        await db.set_trace_callback(statements.append)

    assert await store.delete_expired(30) == 5

    delete_statements = [
        statement
        for statement in statements
        if statement.startswith("DELETE FROM results_meta WHERE id IN")
    ]
    assert len(delete_statements) == 3
    async with get_db("results_meta.db") as db:
        cursor = await db.execute("SELECT COUNT(*) FROM results_meta")
        assert (await cursor.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_prune_excess_per_job_removes_oldest_runs(store):
    from scrapeyard.storage.database import get_db

    run_ids = []
    for index in range(3):
        meta = await store.save_result(
            "job-prune",
            [{"url": f"http://example.com/{index}"}],
            run_id=f"run-{index}",
        )
        run_ids.append(meta.run_id)

    async with get_db("results_meta.db") as db:
        for index, run_id in enumerate(run_ids):
            created_at = (datetime.now(timezone.utc) - timedelta(hours=3 - index)).isoformat()
            await db.execute(
                "UPDATE results_meta SET created_at = ? WHERE run_id = ?",
                (created_at, run_id),
            )
        await db.commit()

    deleted = await store.prune_excess_per_job(2)

    assert deleted == 1
    with pytest.raises(KeyError):
        await store.get_result("job-prune", run_id="run-0")
    payload = await store.get_result("job-prune", run_id="run-2")
    assert payload.run_id == "run-2"


@pytest.mark.asyncio
async def test_prune_excess_per_job_offloads_directory_removal(store):
    from scrapeyard.storage.database import get_db

    oldest = await store.save_result(
        "job-prune-thread",
        [{"url": "http://example.com/old"}],
        run_id="run-old",
    )
    await store.save_result(
        "job-prune-thread",
        [{"url": "http://example.com/new"}],
        run_id="run-new",
    )

    async with get_db("results_meta.db") as db:
        await db.execute(
            "UPDATE results_meta SET created_at = ? WHERE run_id = ?",
            ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(), "run-old"),
        )
        await db.execute(
            "UPDATE results_meta SET created_at = ? WHERE run_id = ?",
            ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), "run-new"),
        )
        await db.commit()

    with patch.object(
        result_store_module.asyncio,
        "to_thread",
        new_callable=AsyncMock,
    ) as mock_to_thread:
        mock_to_thread.side_effect = _run_to_thread
        deleted = await store.prune_excess_per_job(1)

    assert deleted == 1
    assert mock_to_thread.await_args == call(
        result_store_module.remove_directories,
        [Path(oldest.file_path)],
    )


@pytest.mark.asyncio
async def test_delete_expired_skips_unsafe_metadata_path(store, tmp_path):
    from scrapeyard.storage.database import get_db

    outside = tmp_path / "outside"
    outside.mkdir()
    old_date = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    async with get_db("results_meta.db") as db:
        await db.execute(
            """INSERT INTO results_meta
               (job_id, project, run_id, status, record_count, file_path, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                "job-unsafe",
                "test-project",
                "run-unsafe",
                "complete",
                1,
                str(outside),
                old_date,
            ),
        )
        await db.commit()

    deleted = await store.delete_expired(30)

    assert deleted == 1
    assert outside.exists()


@pytest.mark.asyncio
async def test_delete_expired_does_not_remove_results_root_from_bad_metadata(store):
    from scrapeyard.storage.database import get_db

    root_marker = store._results_dir / "keep.txt"
    store._results_dir.mkdir(parents=True)
    root_marker.write_text("keep", encoding="utf-8")
    old_date = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    async with get_db("results_meta.db") as db:
        await db.execute(
            """INSERT INTO results_meta
               (job_id, project, run_id, status, record_count, file_path, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                "job-root",
                "test-project",
                "run-root",
                "complete",
                1,
                str(store._results_dir),
                old_date,
            ),
        )
        await db.commit()

    deleted = await store.delete_expired(30)

    assert deleted == 1
    assert root_marker.read_text(encoding="utf-8") == "keep"


@pytest.mark.asyncio
async def test_reconciliation_keeps_metadata_backed_result_without_parent_job(store):
    meta = await store.save_result("job-valid", {"ok": True}, run_id="run-valid")
    _backdate_tree(Path(meta.file_path))

    report = await store.reconcile_artifacts(
        grace_seconds=86400, dry_run=False, now=NOW
    )

    assert report.metadata_rows_inspected == 1
    assert report.valid_artifacts == 1
    assert report.orphan_candidates == 0
    assert Path(meta.file_path).is_dir()


@pytest.mark.asyncio
async def test_reconciliation_dry_run_then_removes_stale_orphan_idempotently(store):
    run_dir = store._results_dir / "test-project" / "test-job" / "run-orphan"
    run_dir.mkdir(parents=True)
    (run_dir / "artifact.bin").write_bytes(b"12345")
    _backdate_tree(run_dir)

    dry_run = await store.reconcile_artifacts(
        grace_seconds=86400, dry_run=True, now=NOW
    )
    assert dry_run.orphan_candidates == 1
    assert dry_run.directories_would_remove == 1
    assert dry_run.directories_removed == 0
    assert run_dir.is_dir()

    removed = await store.reconcile_artifacts(
        grace_seconds=86400, dry_run=False, now=NOW
    )
    converged = await store.reconcile_artifacts(
        grace_seconds=86400, dry_run=False, now=NOW
    )

    assert removed.directories_removed == 1
    assert removed.removed_bytes == 5
    assert not run_dir.exists()
    assert converged.directories_removed == 0
    assert converged.removed_bytes == 0


@pytest.mark.asyncio
async def test_reconciliation_skips_recent_orphan(store):
    run_dir = store._results_dir / "test-project" / "test-job" / "run-recent"
    run_dir.mkdir(parents=True)
    (run_dir / "debug.txt").write_text("recent", encoding="utf-8")

    report = await store.reconcile_artifacts(
        grace_seconds=86400, dry_run=False, now=NOW
    )

    assert report.orphan_candidates == 0
    assert report.recent_candidates_skipped == 1
    assert run_dir.is_dir()


@pytest.mark.asyncio
async def test_reconciliation_skips_stale_active_run(store):
    async def active(project: str, job_name: str, run_id: str) -> bool:
        return (project, job_name, run_id) == (
            "test-project",
            "test-job",
            "run-active",
        )

    protected_store = LocalResultStore(str(store._results_dir), _lookup, active)
    run_dir = store._results_dir / "test-project" / "test-job" / "run-active"
    run_dir.mkdir(parents=True)
    _backdate_tree(run_dir)

    report = await protected_store.reconcile_artifacts(
        grace_seconds=86400, dry_run=False, now=NOW
    )

    assert report.active_run_candidates_skipped == 1
    assert report.directories_removed == 0
    assert run_dir.is_dir()


@pytest.mark.asyncio
async def test_reconciliation_rechecks_active_state_before_removal(store):
    checks = 0

    async def racing_active(_project: str, _job_name: str, _run_id: str) -> bool:
        nonlocal checks
        checks += 1
        return checks == 2

    protected_store = LocalResultStore(str(store._results_dir), _lookup, racing_active)
    run_dir = store._results_dir / "test-project" / "test-job" / "run-race"
    run_dir.mkdir(parents=True)
    _backdate_tree(run_dir)

    report = await protected_store.reconcile_artifacts(
        grace_seconds=86400, dry_run=False, now=NOW
    )

    assert checks == 2
    assert report.active_run_race_candidates_skipped == 1
    assert run_dir.is_dir()


@pytest.mark.asyncio
async def test_metadata_appearing_after_scan_prevents_orphan_removal(store):
    inserted = False
    run_dir = store._results_dir / "test-project" / "test-job" / "run-indexed-race"
    run_dir.mkdir(parents=True)
    (run_dir / "results.json").write_text('{"ok":true}', encoding="utf-8")
    _backdate_tree(run_dir)

    async def insert_metadata_once(
        _project: str, _job_name: str, _run_id: str
    ) -> bool:
        nonlocal inserted
        if not inserted:
            inserted = True
            await _insert_metadata(
                job_id="job-indexed-race",
                run_id="run-indexed-race",
                file_path=run_dir,
            )
        return False

    protected_store = LocalResultStore(
        str(store._results_dir), _lookup, insert_metadata_once
    )
    report = await protected_store.reconcile_artifacts(
        grace_seconds=86400, dry_run=False, now=NOW
    )

    assert report.metadata_race_candidates_skipped == 1
    assert run_dir.is_dir()


@pytest.mark.asyncio
async def test_metadata_race_lookup_uses_project_run_index(store):
    async with get_db("results_meta.db") as db:
        cursor = await db.execute(
            """EXPLAIN QUERY PLAN
               SELECT file_path FROM results_meta
               WHERE project = ? AND run_id = ?""",
            ("test-project", "run-indexed"),
        )
        plan = " ".join(str(row[3]) for row in await cursor.fetchall())

    assert "idx_results_meta_project_run" in plan


@pytest.mark.asyncio
async def test_reconciliation_removes_only_exact_stale_atomic_temp(store):
    meta = await store.save_result("job-temp", {"ok": True}, run_id="run-temp")
    run_dir = Path(meta.file_path)
    exact = run_dir / f".results.json.123.{('a' * 32)}.tmp"
    lookalike = run_dir / ".arbitrary.123.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.tmp"
    exact.write_bytes(b"temp")
    lookalike.write_bytes(b"keep")
    _backdate_tree(run_dir)

    dry_run = await store.reconcile_artifacts(
        grace_seconds=86400, dry_run=True, now=NOW
    )
    assert dry_run.stale_temporary_candidates == 1
    assert dry_run.files_would_remove == 1
    assert exact.exists()

    removed = await store.reconcile_artifacts(
        grace_seconds=86400, dry_run=False, now=NOW
    )

    assert removed.files_removed == 1
    assert removed.removed_bytes == 4
    assert not exact.exists()
    assert lookalike.read_bytes() == b"keep"


@pytest.mark.asyncio
async def test_reconciliation_skips_temp_in_recent_run(store):
    meta = await store.save_result("job-temp-recent", {"ok": True}, run_id="run-temp")
    run_dir = Path(meta.file_path)
    temp = run_dir / f".results.json.123.{('b' * 32)}.tmp"
    temp.write_bytes(b"old")
    old = (NOW - timedelta(days=2)).timestamp()
    os.utime(temp, (old, old))

    report = await store.reconcile_artifacts(
        grace_seconds=86400, dry_run=False, now=NOW
    )

    assert report.stale_temporary_candidates == 0
    assert report.recent_candidates_skipped == 1
    assert temp.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("contents", "field"),
    [(None, "missing_result_files"), ("not json", "corrupt_result_files")],
)
async def test_reconciliation_reports_missing_and_corrupt_metadata_artifacts(
    store, contents, field
):
    run_dir = store._results_dir / "test-project" / "test-job" / f"run-{field}"
    run_dir.mkdir(parents=True)
    if contents is not None:
        (run_dir / "results.json").write_text(contents, encoding="utf-8")
    await _insert_metadata(job_id=f"job-{field}", run_id=f"run-{field}", file_path=run_dir)

    report = await store.reconcile_artifacts(
        grace_seconds=86400, dry_run=False, now=NOW
    )

    assert getattr(report, field) == 1
    assert report.failure_count == 1
    assert run_dir.is_dir()


@pytest.mark.asyncio
async def test_reconciliation_reports_unreadable_artifact_without_deleting(
    store, monkeypatch
):
    meta = await store.save_result("job-io", {"ok": True}, run_id="run-io")
    real_read = result_store_module.read_json_file_no_follow

    def fail_read(path):
        if Path(path) == Path(meta.file_path) / "results.json":
            raise OSError("sensitive path")
        return real_read(path)

    monkeypatch.setattr(result_store_module, "read_json_file_no_follow", fail_read)
    report = await store.reconcile_artifacts(
        grace_seconds=86400, dry_run=False, now=NOW
    )

    assert report.unreadable_result_files == 1
    assert report.artifact_failures[0].error_type == "OSError"
    assert Path(meta.file_path).is_dir()


@pytest.mark.asyncio
async def test_reconciliation_reports_unsafe_outside_metadata_and_preserves_target(
    store, tmp_path
):
    outside = tmp_path / "outside"
    outside.mkdir()
    result = outside / "results.json"
    result.write_text('{"secret":true}', encoding="utf-8")
    await _insert_metadata(job_id="job-outside", run_id="run-outside", file_path=outside)

    report = await store.reconcile_artifacts(
        grace_seconds=86400, dry_run=False, now=NOW
    )

    assert report.unsafe_metadata_paths == 1
    assert result.read_text(encoding="utf-8") == '{"secret":true}'


@pytest.mark.asyncio
async def test_reconciliation_never_follows_run_or_result_symlinks(store, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_result = outside / "results.json"
    outside_result.write_text('{"keep":true}', encoding="utf-8")
    job_dir = store._results_dir / "test-project" / "test-job"
    job_dir.mkdir(parents=True)
    (job_dir / "run-link").symlink_to(outside, target_is_directory=True)
    result_link_run = job_dir / "run-result-link"
    result_link_run.mkdir()
    (result_link_run / "results.json").symlink_to(outside_result)
    await _insert_metadata(
        job_id="job-link", run_id="run-result-link", file_path=result_link_run
    )

    report = await store.reconcile_artifacts(
        grace_seconds=86400, dry_run=False, now=NOW
    )

    assert report.unsafe_metadata_paths == 1
    assert report.malformed_entries_ignored >= 1
    assert outside_result.read_text(encoding="utf-8") == '{"keep":true}'


@pytest.mark.asyncio
async def test_reconciliation_never_follows_project_job_or_temp_symlinks(
    store, tmp_path
):
    outside = tmp_path / "outside-tree"
    outside.mkdir()
    outside_marker = outside / "keep.txt"
    outside_marker.write_text("keep", encoding="utf-8")
    store._results_dir.mkdir(parents=True)
    (store._results_dir / "project-link").symlink_to(
        outside, target_is_directory=True
    )
    project = store._results_dir / "test-project"
    project.mkdir()
    (project / "job-link").symlink_to(outside, target_is_directory=True)
    meta = await store.save_result("job-temp-link", {"ok": True}, run_id="run-temp-link")
    temp_target = tmp_path / "outside-temp"
    temp_target.write_bytes(b"keep-temp")
    temp_link = Path(meta.file_path) / f".results.json.123.{('c' * 32)}.tmp"
    temp_link.symlink_to(temp_target)
    _backdate_tree(Path(meta.file_path))

    report = await store.reconcile_artifacts(
        grace_seconds=86400, dry_run=False, now=NOW
    )

    assert report.malformed_entries_ignored >= 2
    assert report.stale_temporary_candidates == 0
    assert temp_link.is_symlink()
    assert outside_marker.read_text(encoding="utf-8") == "keep"
    assert temp_target.read_bytes() == b"keep-temp"


@pytest.mark.asyncio
async def test_reconciliation_skips_temp_in_stale_active_run(store):
    async def active(_project: str, _job_name: str, run_id: str) -> bool:
        return run_id == "run-active-temp"

    protected_store = LocalResultStore(str(store._results_dir), _lookup, active)
    meta = await protected_store.save_result(
        "job-active-temp", {"ok": True}, run_id="run-active-temp"
    )
    temp = Path(meta.file_path) / f".results.json.123.{('d' * 32)}.tmp"
    temp.write_bytes(b"keep")
    _backdate_tree(Path(meta.file_path))

    report = await protected_store.reconcile_artifacts(
        grace_seconds=86400, dry_run=False, now=NOW
    )

    assert report.stale_temporary_candidates == 1
    assert report.active_run_candidates_skipped == 1
    assert temp.exists()


@pytest.mark.asyncio
async def test_result_directory_containment_rejects_root_wrong_depth_and_traversal(store):
    root = store._results_dir

    with pytest.raises(ValueError, match="project/job/run"):
        store._checked_result_dir(str(root))
    with pytest.raises(ValueError, match="project/job/run"):
        store._checked_result_dir(str(root / "project" / "job"))
    with pytest.raises(ValueError, match="project/job/run"):
        store._checked_result_dir(str(root / "project" / "job" / "run" / "extra"))
    with pytest.raises(ValueError, match="outside results_dir"):
        store._checked_result_dir(str(root / "project" / ".." / "job" / "run"))


@pytest.mark.asyncio
async def test_reconciliation_ignores_non_directory_at_run_depth(store):
    run_file = store._results_dir / "test-project" / "test-job" / "not-a-run"
    run_file.parent.mkdir(parents=True)
    run_file.write_text("keep", encoding="utf-8")
    _backdate_tree(run_file)

    report = await store.reconcile_artifacts(
        grace_seconds=86400, dry_run=False, now=NOW
    )

    assert report.filesystem_run_directories_inspected == 0
    assert report.malformed_entries_ignored == 1
    assert run_file.read_text(encoding="utf-8") == "keep"


@pytest.mark.asyncio
async def test_reconciliation_partial_removal_failure_is_reported_and_retryable(
    store, monkeypatch
):
    first = store._results_dir / "test-project" / "test-job" / "run-a"
    second = store._results_dir / "test-project" / "test-job" / "run-b"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    (first / "one").write_bytes(b"1")
    (second / "two").write_bytes(b"22")
    _backdate_tree(first)
    _backdate_tree(second)
    real_remove = store._remove_run_candidate

    def partial_failure(candidate, cutoff):
        if candidate.identity.run_id == "run-a":
            raise OSError("sensitive path")
        return real_remove(candidate, cutoff)

    monkeypatch.setattr(store, "_remove_run_candidate", partial_failure)
    report = await store.reconcile_artifacts(
        grace_seconds=86400, dry_run=False, now=NOW
    )

    assert report.directories_removed == 1
    assert report.removed_bytes == 2
    assert report.operation_failures[0].action == "remove_run"
    assert report.operation_failures[0].error_type == "OSError"
    assert first.is_dir()
    assert not second.exists()
