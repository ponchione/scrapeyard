"""Tests for LocalResultStore save and retrieval."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest
from unittest.mock import ANY, AsyncMock, call, patch

import scrapeyard.storage.result_store as result_store_module
from scrapeyard.common.budgets import BudgetExceeded
from scrapeyard.storage.database import get_db
from scrapeyard.storage.database import init_db
from scrapeyard.storage.result_store import LocalResultStore, SaveResultMeta
from scrapeyard.storage.types import ResultArtifactReadError


async def _lookup(job_id: str) -> tuple[str, str]:
    """Stub job lookup returning fixed project/name."""
    return ("acme", "scrape-prices")


async def _unsafe_lookup(job_id: str) -> tuple[str, str]:
    """Stub job lookup returning unsafe path components."""
    return ("../outside", "scrape-prices")


async def _run_to_thread(func, *args, **kwargs):
    return func(*args, **kwargs)


@pytest.fixture()
async def store(tmp_path):
    await init_db(str(tmp_path / "db"))
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    return LocalResultStore(str(results_dir), _lookup)


async def test_save_and_get_json(store):
    data = [{"price": 9.99}, {"price": 19.99}]
    meta = await store.save_result("j-1", data)
    run_id = meta.run_id

    result = await store.get_result("j-1", run_id)
    assert result.data == data
    assert result.run_id == run_id


async def test_get_latest_without_run_id(store):
    data1 = [{"v": 1}]
    data2 = [{"v": 2}]
    await store.save_result("j-1", data1)
    await store.save_result("j-1", data2)

    result = await store.get_result("j-1")
    # Latest should be data2
    assert result.data == data2


async def test_get_latest_metadata_includes_project_and_matches_payload_selection(store):
    await store.save_result("j-1", [{"v": 1}], run_id="run-1")
    await store.save_result("j-1", [{"v": 2}], run_id="run-2")

    metadata = await store.get_result_metadata("j-1")
    payload = await store.get_result("j-1")

    assert metadata is not None
    assert metadata.project == "acme"
    assert metadata.run_id == payload.run_id == "run-2"


async def test_get_result_not_found(store):
    with pytest.raises(KeyError, match="No results found"):
        await store.get_result("j-1")


async def test_get_result_specific_run_not_found(store):
    with pytest.raises(KeyError, match="No results found"):
        await store.get_result("j-1", "nonexistent-run")


async def test_save_result_returns_meta(store):
    data = [{"price": 9.99}, {"price": 19.99}]
    meta = await store.save_result("j-1", data)

    assert isinstance(meta, SaveResultMeta)
    assert isinstance(meta.run_id, str)
    assert meta.file_path.endswith(meta.run_id)
    assert meta.record_count is None  # no record_count passed
    assert meta.serialized_bytes == len(result_store_module.serialize_json_bytes(data))


async def test_save_result_with_record_count(store):
    data = [{"price": 9.99}, {"price": 19.99}]
    meta = await store.save_result("j-1", data, record_count=2)

    assert meta.record_count == 2


async def test_save_result_persists_explicit_status(store):
    meta = await store.save_result("j-1", [{"price": 9.99}], status="partial")
    payload = await store.get_result("j-1", meta.run_id)

    async with get_db("results_meta.db") as db:
        cursor = await db.execute(
            "SELECT status FROM results_meta WHERE job_id=? AND run_id=?",
            ("j-1", meta.run_id),
        )
        row = await cursor.fetchone()

    assert row["status"] == "partial"
    assert payload.status == "partial"


async def test_get_result_metadata_does_not_read_artifact(store):
    meta = await store.save_result(
        "j-1",
        [{"price": 9.99}],
        run_id="run-1",
        status="partial",
        record_count=1,
    )
    (Path(meta.file_path) / "results.json").unlink()

    metadata = await store.get_result_metadata("j-1", "run-1")

    assert metadata is not None
    assert metadata.job_id == "j-1"
    assert metadata.run_id == "run-1"
    assert metadata.status == "partial"
    assert metadata.record_count == 1
    assert metadata.file_path == meta.file_path
    assert await store.get_result_metadata("j-1", "missing") is None


async def test_save_result_reuses_explicit_run_id(store):
    first = await store.save_result("j-1", [{"price": 9.99}], run_id="run-1")
    second = await store.save_result(
        "j-1", [{"price": 19.99}], run_id="run-1"
    )

    result = await store.get_result("j-1", "run-1")

    assert first.run_id == "run-1"
    assert second.run_id == "run-1"
    assert result.data == [{"price": 19.99}]


async def test_save_result_restores_existing_artifact_when_metadata_commit_fails(
    store,
    monkeypatch,
):
    await store.save_result(
        "j-1",
        [{"version": "old"}],
        run_id="run-1",
        status="complete",
        record_count=1,
    )
    async with get_db("results_meta.db") as db:
        connection_type = type(db)

    async def fail_commit(_connection) -> None:
        raise OSError("commit failed")

    with monkeypatch.context() as context:
        context.setattr(connection_type, "commit", fail_commit)
        with pytest.raises(OSError, match="commit failed"):
            await store.save_result(
                "j-1",
                [{"version": "new"}],
                run_id="run-1",
                status="partial",
                record_count=99,
            )

    result = await store.get_result("j-1", "run-1")
    metadata = await store.get_result_metadata("j-1", "run-1")
    assert result.data == [{"version": "old"}]
    assert result.status == "complete"
    assert metadata is not None
    assert metadata.status == "complete"
    assert metadata.record_count == 1


async def test_save_result_removes_new_artifact_when_metadata_commit_fails(
    store,
    monkeypatch,
):
    async with get_db("results_meta.db") as db:
        connection_type = type(db)

    async def fail_commit(_connection) -> None:
        raise OSError("commit failed")

    with monkeypatch.context() as context:
        context.setattr(connection_type, "commit", fail_commit)
        with pytest.raises(OSError, match="commit failed"):
            await store.save_result(
                "j-1",
                [{"version": "new"}],
                run_id="run-new",
            )

    run_dir = store._results_dir / "acme" / "scrape-prices" / "run-new"
    assert not (run_dir / "results.json").exists()
    with pytest.raises(KeyError):
        await store.get_result("j-1", "run-new")


async def test_run_id_format(store):
    meta = await store.save_result("j-1", [{"a": 1}])
    run_id = meta.run_id
    # Format: YYYYMMDD-HHMMSS-{16 hex chars}
    parts = run_id.split("-")
    assert len(parts) == 3
    assert len(parts[0]) == 8  # YYYYMMDD
    assert len(parts[1]) == 6  # HHMMSS
    assert len(parts[2]) == 16  # short uuid


async def test_save_result_writes_json_file(store, tmp_path):
    data = [{"price": 9.99}]
    meta = await store.save_result("j-1", data)
    run_id = meta.run_id

    results_dir = tmp_path / "results" / "acme" / "scrape-prices" / run_id
    json_path = results_dir / "results.json"
    assert json_path.exists()


async def test_save_result_rejects_unsafe_job_path_components(tmp_path):
    await init_db(str(tmp_path / "db"))
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    store = LocalResultStore(str(results_dir), _unsafe_lookup)

    with pytest.raises(ValueError, match="Unsafe"):
        await store.save_result("j-1", [{"price": 9.99}], run_id="run-1")

    assert not (tmp_path / "outside").exists()


async def test_save_result_rejects_symlinked_run_dir_outside_results_dir(store, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    run_dir = store._results_dir / "acme" / "scrape-prices" / "run-1"
    run_dir.parent.mkdir(parents=True)
    run_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="outside results_dir"):
        await store.save_result("j-1", [{"price": 9.99}], run_id="run-1")

    assert not (outside / "results.json").exists()


async def test_save_result_offloads_filesystem_work(store):
    data = [{"price": 9.99}]
    run_dir = store._results_dir / "acme" / "scrape-prices" / "run-1"

    with patch.object(
        result_store_module.asyncio,
        "to_thread",
        new_callable=AsyncMock,
    ) as mock_to_thread:
        mock_to_thread.side_effect = _run_to_thread
        await store.save_result("j-1", data, run_id="run-1")

    assert mock_to_thread.await_args_list == [
        call(result_store_module.ensure_directory, run_dir),
        call(
            result_store_module.read_bytes_file_no_follow,
            run_dir / "results.json",
        ),
        call(
            result_store_module.write_json_file_bounded,
            run_dir / "results.json",
            data,
            store._max_serialized_result_bytes,
            ANY,
        ),
    ]


async def test_save_result_preserves_existing_artifacts(store):
    run_dir = store._results_dir / "acme" / "scrape-prices" / "run-1"
    artifacts_dir = run_dir / "artifacts" / "example.com"
    artifacts_dir.mkdir(parents=True)
    screenshot = artifacts_dir / "dynamic-main.png"
    screenshot.write_bytes(b"png")

    await store.save_result("j-1", [{"price": 9.99}], run_id="run-1")

    assert screenshot.read_bytes() == b"png"


async def test_save_result_writes_distinct_run_artifacts_concurrently(
    store,
    monkeypatch,
):
    real_write = result_store_module.write_json_file_bounded
    state_lock = threading.Lock()
    both_writing = threading.Event()
    release = threading.Event()
    active = 0
    max_active = 0

    def blocking_write(path, data, max_bytes, checkpoint):
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
            if active == 2:
                both_writing.set()
        release.wait(timeout=10)
        try:
            return real_write(path, data, max_bytes, checkpoint)
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(
        result_store_module,
        "write_json_file_bounded",
        blocking_write,
    )
    tasks = [
        asyncio.create_task(
            store.save_result("j-1", {"run": run_id}, run_id=run_id)
        )
        for run_id in ("run-one", "run-two")
    ]
    try:
        assert await asyncio.to_thread(both_writing.wait, 5)
    finally:
        release.set()
    await asyncio.gather(*tasks)

    assert max_active == 2
    assert store._save_locks == {}


async def test_save_result_serializes_same_run_and_releases_keyed_lock(
    store,
    monkeypatch,
):
    real_write = result_store_module.write_json_file_bounded
    release_first = threading.Event()
    first_writing = threading.Event()
    state_lock = threading.Lock()
    calls = 0
    active = 0
    max_active = 0

    def blocking_first_write(path, data, max_bytes, checkpoint):
        nonlocal calls, active, max_active
        with state_lock:
            calls += 1
            call_number = calls
            active += 1
            max_active = max(max_active, active)
        if call_number == 1:
            first_writing.set()
            release_first.wait(timeout=10)
        try:
            return real_write(path, data, max_bytes, checkpoint)
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(
        result_store_module,
        "write_json_file_bounded",
        blocking_first_write,
    )
    first = asyncio.create_task(
        store.save_result("j-1", {"version": 1}, run_id="same-run")
    )
    assert await asyncio.to_thread(first_writing.wait, 5)
    second = asyncio.create_task(
        store.save_result("j-1", {"version": 2}, run_id="same-run")
    )
    run_dir = store._results_dir / "acme" / "scrape-prices" / "same-run"
    async def wait_for_second_user() -> None:
        while True:
            entry = store._save_locks.get(run_dir)
            if entry is not None and entry.users == 2:
                return
            await asyncio.sleep(0.001)

    try:
        await asyncio.wait_for(wait_for_second_user(), timeout=5)
        assert calls == 1
    finally:
        release_first.set()
    await asyncio.gather(first, second)

    assert calls == 2
    assert max_active == 1
    assert (await store.get_result("j-1", "same-run")).data == {"version": 2}
    assert store._save_locks == {}


async def test_save_result_allows_exact_serialized_byte_boundary(store):
    data = {"value": "café"}
    exact_size = len(result_store_module.serialize_json_bytes(data))

    meta = await store.save_result(
        "j-1",
        data,
        run_id="run-exact",
        max_serialized_bytes=exact_size,
    )

    assert meta.serialized_bytes == exact_size
    assert (Path(meta.file_path) / "results.json").stat().st_size == exact_size


async def test_save_result_rejects_one_byte_over_before_file_or_metadata(store):
    data = {"value": "café"}
    exact_size = len(result_store_module.serialize_json_bytes(data))

    with pytest.raises(BudgetExceeded) as exc_info:
        await store.save_result(
            "j-1",
            data,
            run_id="run-over",
            max_serialized_bytes=exact_size - 1,
        )

    assert "serialized_result_bytes" in str(exc_info.value)
    run_dir = store._results_dir / "acme" / "scrape-prices" / "run-over"
    assert not run_dir.exists()
    async with get_db("results_meta.db") as db:
        row = await (
            await db.execute("SELECT id FROM results_meta WHERE run_id='run-over'")
        ).fetchone()
    assert row is None


async def test_oversized_save_never_builds_a_complete_json_byte_buffer(
    store,
    monkeypatch,
):
    data = {"escaped": '"\\\n' * 1000, "multibyte": "é" * 1000}

    def forbidden_whole_document_encoder(_data):
        raise AssertionError("whole-document byte encoding must not be used")

    monkeypatch.setattr(
        result_store_module,
        "serialize_json_bytes",
        forbidden_whole_document_encoder,
    )

    with pytest.raises(BudgetExceeded):
        await store.save_result(
            "j-1",
            data,
            run_id="run-stream-over",
            max_serialized_bytes=128,
        )

    run_dir = store._results_dir / "acme" / "scrape-prices" / "run-stream-over"
    assert not run_dir.exists()


async def test_save_result_serialization_failure_creates_no_run_directory(store):
    class BrokenValue:
        def __str__(self):
            raise RuntimeError("cannot serialize")

    with pytest.raises(RuntimeError, match="cannot serialize"):
        await store.save_result(
            "j-1",
            {"value": BrokenValue()},
            run_id="run-serialization-error",
        )

    run_dir = (
        store._results_dir
        / "acme"
        / "scrape-prices"
        / "run-serialization-error"
    )
    assert not run_dir.exists()


async def test_save_result_disk_failure_leaves_no_temp_file_or_metadata(store, monkeypatch):
    def fail_replace(*_args):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("scrapeyard.storage.filesystem.os.replace", fail_replace)

    with pytest.raises(OSError, match="No space"):
        await store.save_result("j-1", {"value": 1}, run_id="run-full")

    run_dir = store._results_dir / "acme" / "scrape-prices" / "run-full"
    assert not (run_dir / "results.json").exists()
    assert list(run_dir.glob("*.tmp")) == []
    assert list(run_dir.glob(".*.tmp")) == []
    async with get_db("results_meta.db") as db:
        row = await (
            await db.execute("SELECT id FROM results_meta WHERE run_id='run-full'")
        ).fetchone()
    assert row is None


async def test_save_result_cancellation_cleans_completed_uncommitted_file(store, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    real_write = result_store_module.write_json_file_bounded

    def delayed_write(path, data, max_bytes, checkpoint):
        started.set()
        release.wait(timeout=2)
        return real_write(path, data, max_bytes, checkpoint)

    monkeypatch.setattr(
        result_store_module,
        "write_json_file_bounded",
        delayed_write,
    )
    task = asyncio.create_task(
        store.save_result("j-1", {"value": 1}, run_id="run-cancelled")
    )
    await asyncio.to_thread(started.wait, 1)
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    run_dir = store._results_dir / "acme" / "scrape-prices" / "run-cancelled"
    assert not (run_dir / "results.json").exists()
    assert list(run_dir.glob(".*.tmp")) == []
    async with get_db("results_meta.db") as db:
        row = await (
            await db.execute("SELECT id FROM results_meta WHERE run_id='run-cancelled'")
        ).fetchone()
    assert row is None


async def test_get_result_offloads_json_read(store):
    data = [{"price": 9.99}]
    meta = await store.save_result("j-1", data, run_id="run-1")
    json_path = store._results_dir / "acme" / "scrape-prices" / "run-1" / "results.json"

    with patch.object(
        result_store_module.asyncio,
        "to_thread",
        new_callable=AsyncMock,
    ) as mock_to_thread:
        mock_to_thread.side_effect = _run_to_thread
        payload = await store.get_result("j-1", meta.run_id)

    assert payload.data == data
    assert mock_to_thread.await_args == call(
        result_store_module.read_json_file_no_follow,
        json_path,
        max_bytes=store._max_serialized_result_bytes,
    )


async def test_get_result_rejects_oversized_artifact_before_decoding(tmp_path):
    await init_db(str(tmp_path / "db"))
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    limited = LocalResultStore(
        str(results_dir),
        _lookup,
        max_serialized_result_bytes=16,
    )
    meta = await limited.save_result("j-1", {}, run_id="run-oversized")
    result_path = Path(meta.file_path) / "results.json"
    result_path.write_bytes(b'{' + b'"value":"' + b'x' * 32 + b'"}')

    with patch("scrapeyard.storage.filesystem.os.fdopen") as fdopen:
        with pytest.raises(ResultArtifactReadError):
            await limited.get_result("j-1", "run-oversized")

    fdopen.assert_not_called()


async def test_get_result_rejects_symlinked_result_file(store, tmp_path):
    meta = await store.save_result("j-1", [{"price": 9.99}], run_id="run-link")
    result_path = Path(meta.file_path) / "results.json"
    outside = tmp_path / "outside.json"
    outside.write_text('{"secret": true}', encoding="utf-8")
    result_path.unlink()
    result_path.symlink_to(outside)

    with pytest.raises(OSError):
        await store.get_result("j-1", meta.run_id)

    assert outside.read_text(encoding="utf-8") == '{"secret": true}'


async def test_get_result_rejects_metadata_path_outside_results_dir(store, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "results.json").write_text('{"secret": true}', encoding="utf-8")

    async with get_db("results_meta.db") as db:
        await db.execute(
            """INSERT INTO results_meta
               (job_id, project, run_id, status, record_count, file_path, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                "j-unsafe",
                "acme",
                "run-unsafe",
                "complete",
                1,
                str(outside),
                "2026-01-01T00:00:00+00:00",
            ),
        )
        await db.commit()

    with pytest.raises(ValueError, match="outside results_dir"):
        await store.get_result("j-unsafe", "run-unsafe")


async def test_get_result_rejects_metadata_path_at_results_root(store):
    async with get_db("results_meta.db") as db:
        await db.execute(
            """INSERT INTO results_meta
               (job_id, project, run_id, status, record_count, file_path, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                "j-root",
                "acme",
                "run-root",
                "complete",
                1,
                str(store._results_dir),
                "2026-01-01T00:00:00+00:00",
            ),
        )
        await db.commit()

    with pytest.raises(ValueError, match="project/job/run"):
        await store.get_result("j-root", "run-root")


async def test_delete_results_offloads_directory_removal(store):
    first = await store.save_result("j-1", [{"price": 9.99}], run_id="run-1")
    second = await store.save_result("j-1", [{"price": 19.99}], run_id="run-2")

    with patch.object(
        result_store_module.asyncio,
        "to_thread",
        new_callable=AsyncMock,
    ) as mock_to_thread:
        mock_to_thread.side_effect = _run_to_thread
        await store.delete_results("j-1")

    assert mock_to_thread.await_count == 1
    assert mock_to_thread.await_args.args[0] is result_store_module.remove_directories
    assert set(mock_to_thread.await_args.args[1]) == {
        Path(first.file_path),
        Path(second.file_path),
    }


async def test_delete_result_removes_metadata_and_run_directory(store):
    meta = await store.save_result("j-1", [{"price": 9.99}], run_id="run-owned")

    assert await store.delete_result("j-1", "run-owned") is True

    assert not Path(meta.file_path).exists()
    with pytest.raises(KeyError):
        await store.get_result("j-1", "run-owned")


async def test_delete_result_removes_unindexed_browser_artifacts_idempotently(store):
    run_dir = store._results_dir / "acme" / "scrape-prices" / "run-unindexed"
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True)
    (artifacts_dir / "page.png").write_bytes(b"debug")

    assert await store.delete_result("j-1", "run-unindexed") is True
    assert not run_dir.exists()
    assert await store.delete_result("j-1", "run-unindexed") is False


async def test_delete_results_removes_unindexed_owned_browser_artifacts(store):
    run_dir = store._results_dir / "acme" / "scrape-prices" / "run-unindexed"
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True)
    (artifacts_dir / "page.png").write_bytes(b"debug")

    await store.delete_results("j-1", owned_run_ids=("run-unindexed",))

    assert not run_dir.exists()


async def test_delete_results_removes_indexed_and_unindexed_owned_runs(store):
    indexed = await store.save_result("j-1", [{"price": 9.99}], run_id="run-indexed")
    unindexed = store._results_dir / "acme" / "scrape-prices" / "run-unindexed"
    (unindexed / "artifacts").mkdir(parents=True)

    await store.delete_results(
        "j-1",
        owned_run_ids=("run-indexed", "run-unindexed"),
    )

    assert not Path(indexed.file_path).exists()
    assert not unindexed.exists()
    assert await store.get_result_metadata("j-1", "run-indexed") is None


async def test_delete_results_preserves_reused_name_results_from_another_job(store):
    retained = await store.save_result(
        "j-retained",
        [{"price": 19.99}],
        run_id="run-retained",
    )
    owned = store._results_dir / "acme" / "scrape-prices" / "run-owned"
    (owned / "artifacts").mkdir(parents=True)

    await store.delete_results("j-1", owned_run_ids=("run-owned",))

    assert not owned.exists()
    assert Path(retained.file_path).is_dir()
    assert await store.get_result_metadata("j-retained", "run-retained") is not None


async def test_delete_results_unindexed_filesystem_failure_is_retryable(store):
    indexed = await store.save_result("j-1", [{"price": 9.99}], run_id="run-indexed")
    unindexed = store._results_dir / "acme" / "scrape-prices" / "run-unindexed"
    (unindexed / "artifacts").mkdir(parents=True)
    real_remove = result_store_module.remove_directories

    with patch.object(
        result_store_module,
        "remove_directories",
        side_effect=PermissionError("injected filesystem fault"),
    ):
        with pytest.raises(PermissionError, match="injected filesystem fault"):
            await store.delete_results(
                "j-1",
                owned_run_ids=("run-indexed", "run-unindexed"),
            )

    assert Path(indexed.file_path).is_dir()
    assert unindexed.is_dir()
    assert await store.get_result_metadata("j-1", "run-indexed") is not None

    with patch.object(result_store_module, "remove_directories", real_remove):
        await store.delete_results(
            "j-1",
            owned_run_ids=("run-indexed", "run-unindexed"),
        )

    assert not Path(indexed.file_path).exists()
    assert not unindexed.exists()
    assert await store.get_result_metadata("j-1", "run-indexed") is None


async def test_delete_results_rejects_symlinked_owned_run(store, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    job_dir = store._results_dir / "acme" / "scrape-prices"
    job_dir.mkdir(parents=True)
    (job_dir / "run-link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlinked result path"):
        await store.delete_results("j-1", owned_run_ids=("run-link",))

    assert marker.read_text(encoding="utf-8") == "keep"


async def test_delete_results_rejects_uncontained_owned_run_id(store, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError, match="Unsafe path component"):
        await store.delete_results("j-1", owned_run_ids=("../outside",))

    assert marker.read_text(encoding="utf-8") == "keep"
