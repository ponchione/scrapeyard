"""Tests for LocalResultStore save and retrieval."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, call, patch

import scrapeyard.storage.result_store as result_store_module
from scrapeyard.storage.database import get_db
from scrapeyard.storage.database import init_db
from scrapeyard.storage.result_store import LocalResultStore, SaveResultMeta


async def _lookup(job_id: str) -> tuple[str, str]:
    """Stub job lookup returning fixed project/name."""
    return ("acme", "scrape-prices")


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


async def test_save_result_with_record_count(store):
    data = [{"price": 9.99}, {"price": 19.99}]
    meta = await store.save_result("j-1", data, record_count=2)

    assert meta.record_count == 2


async def test_save_result_persists_explicit_status(store):
    meta = await store.save_result("j-1", [{"price": 9.99}], status="partial")

    async with get_db("results_meta.db") as db:
        cursor = await db.execute(
            "SELECT status FROM results_meta WHERE job_id=? AND run_id=?",
            ("j-1", meta.run_id),
        )
        row = await cursor.fetchone()

    assert row == ("partial",)


async def test_save_result_reuses_explicit_run_id(store):
    first = await store.save_result("j-1", [{"price": 9.99}], run_id="run-1")
    second = await store.save_result(
        "j-1", [{"price": 19.99}], run_id="run-1"
    )

    result = await store.get_result("j-1", "run-1")

    assert first.run_id == "run-1"
    assert second.run_id == "run-1"
    assert result.data == [{"price": 19.99}]


async def test_run_id_format(store):
    meta = await store.save_result("j-1", [{"a": 1}])
    run_id = meta.run_id
    # Format: YYYYMMDD-HHMMSS-{8 hex chars}
    parts = run_id.split("-")
    assert len(parts) == 3
    assert len(parts[0]) == 8  # YYYYMMDD
    assert len(parts[1]) == 6  # HHMMSS
    assert len(parts[2]) == 8  # short uuid


async def test_save_result_writes_json_file(store, tmp_path):
    data = [{"price": 9.99}]
    meta = await store.save_result("j-1", data)
    run_id = meta.run_id

    results_dir = tmp_path / "results" / "acme" / "scrape-prices" / run_id
    json_path = results_dir / "results.json"
    assert json_path.exists()


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
        call(result_store_module.prepare_directory, run_dir),
        call(result_store_module.write_json_file, run_dir / "results.json", data),
    ]


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
        result_store_module.read_json_file,
        json_path,
    )


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
        first.file_path,
        second.file_path,
    }
