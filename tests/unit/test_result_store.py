"""Tests for LocalResultStore save and retrieval."""

from __future__ import annotations

import pytest

from scrapeyard.storage.database import get_db
from scrapeyard.storage.database import init_db
from scrapeyard.storage.result_store import LocalResultStore, SaveResultMeta


async def _lookup(job_id: str) -> tuple[str, str]:
    """Stub job lookup returning fixed project/name."""
    return ("acme", "scrape-prices")


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
