"""Tests for LocalResultStore save and retrieval."""

from __future__ import annotations

import pytest

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
    meta = await store.save_result("j-1", data, "json")
    run_id = meta.run_id

    result = await store.get_result("j-1", run_id)
    assert result == data


async def test_save_markdown(store, tmp_path):
    md = "# Prices\n- $9.99\n- $19.99"
    meta = await store.save_result("j-1", md, "markdown")
    run_id = meta.run_id

    result = await store.get_result("j-1", run_id)
    assert result == md


async def test_save_html(store):
    html = "<html><body>hello</body></html>"
    meta = await store.save_result("j-1", html, "html")
    run_id = meta.run_id

    result = await store.get_result("j-1", run_id)
    assert result == html


async def test_save_json_markdown(store, tmp_path):
    data = [{"price": 9.99}]
    meta = await store.save_result("j-1", data, "json+markdown")
    run_id = meta.run_id

    # get_result prefers JSON
    result = await store.get_result("j-1", run_id)
    assert result == data

    # Verify markdown file also exists on disk
    results_dir = tmp_path / "results" / "acme" / "scrape-prices" / run_id
    assert (results_dir / "results.md").exists()
    assert (results_dir / "results.json").exists()


async def test_get_latest_without_run_id(store):
    data1 = [{"v": 1}]
    data2 = [{"v": 2}]
    await store.save_result("j-1", data1, "json")
    await store.save_result("j-1", data2, "json")

    result = await store.get_result("j-1")
    # Latest should be data2
    assert result == data2


async def test_get_result_not_found(store):
    with pytest.raises(KeyError, match="No results found"):
        await store.get_result("j-1")


async def test_get_result_specific_run_not_found(store):
    with pytest.raises(KeyError, match="No results found"):
        await store.get_result("j-1", "nonexistent-run")


async def test_unsupported_format(store):
    with pytest.raises(ValueError, match="Unsupported format"):
        await store.save_result("j-1", {}, "xml")


async def test_save_result_returns_meta(store):
    data = [{"price": 9.99}, {"price": 19.99}]
    meta = await store.save_result("j-1", data, "json")

    assert isinstance(meta, SaveResultMeta)
    assert isinstance(meta.run_id, str)
    assert meta.file_path.endswith(meta.run_id)
    assert meta.record_count is None  # no record_count passed


async def test_save_result_with_record_count(store):
    data = [{"price": 9.99}, {"price": 19.99}]
    meta = await store.save_result("j-1", data, "json", record_count=2)

    assert meta.record_count == 2


async def test_run_id_format(store):
    meta = await store.save_result("j-1", [{"a": 1}], "json")
    run_id = meta.run_id
    # Format: YYYYMMDD-HHMMSS-{8 hex chars}
    parts = run_id.split("-")
    assert len(parts) == 3
    assert len(parts[0]) == 8  # YYYYMMDD
    assert len(parts[1]) == 6  # HHMMSS
    assert len(parts[2]) == 8  # short uuid
