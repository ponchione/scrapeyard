"""Tests for scrapeyard.storage.filesystem — directory and JSON file helpers."""

from datetime import datetime

import pytest

from scrapeyard.storage.filesystem import (
    ensure_directory,
    read_json_file,
    remove_directories,
    write_json_file,
)


def test_ensure_directory_creates_dir(tmp_path):
    target = tmp_path / "new_dir"
    ensure_directory(target)
    assert target.is_dir()
    assert list(target.iterdir()) == []


def test_ensure_directory_preserves_existing_contents(tmp_path):
    target = tmp_path / "existing"
    target.mkdir()
    (target / "old_file.txt").write_text("stale")
    (target / "subdir").mkdir()
    (target / "subdir" / "nested.txt").write_text("nested")

    ensure_directory(target)

    assert target.is_dir()
    assert (target / "old_file.txt").read_text() == "stale"
    assert (target / "subdir" / "nested.txt").read_text() == "nested"


def test_write_and_read_json_round_trip(tmp_path):
    filepath = tmp_path / "data.json"
    data = {"key": "value", "numbers": [1, 2, 3]}
    write_json_file(filepath, data)
    result = read_json_file(filepath)
    assert result == data


def test_write_json_non_serializable_uses_default_str(tmp_path):
    filepath = tmp_path / "dates.json"
    dt = datetime(2024, 1, 15, 10, 30, 0)
    write_json_file(filepath, {"ts": dt})
    result = read_json_file(filepath)
    assert result["ts"] == str(dt)


def test_write_json_file_uses_unique_temp_paths(tmp_path, monkeypatch):
    filepath = tmp_path / "data.json"
    seen_sources = []

    from scrapeyard.storage import filesystem

    real_replace = filesystem.os.replace

    def recording_replace(src, dst):
        seen_sources.append(src)
        real_replace(src, dst)

    monkeypatch.setattr(filesystem.os, "replace", recording_replace)

    write_json_file(filepath, {"value": 1})
    write_json_file(filepath, {"value": 2})

    assert len(seen_sources) == 2
    assert seen_sources[0] != seen_sources[1]
    assert filepath.with_name(filepath.name + ".tmp") not in seen_sources
    assert read_json_file(filepath) == {"value": 2}


def test_write_json_file_cleans_temp_file_on_replace_failure(tmp_path, monkeypatch):
    filepath = tmp_path / "data.json"

    from scrapeyard.storage import filesystem

    def failing_replace(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(filesystem.os, "replace", failing_replace)

    with pytest.raises(OSError, match="replace failed"):
        write_json_file(filepath, {"value": 1})

    assert list(tmp_path.iterdir()) == []


def test_read_json_missing_file_raises(tmp_path):
    missing = tmp_path / "no_such_file.json"
    with pytest.raises(FileNotFoundError):
        read_json_file(missing)


def test_remove_directories_removes_existing(tmp_path):
    d1 = tmp_path / "a"
    d2 = tmp_path / "b"
    d1.mkdir()
    d2.mkdir()
    (d1 / "file.txt").write_text("content")

    remove_directories([d1, d2])

    assert not d1.exists()
    assert not d2.exists()


def test_remove_directories_ignores_missing(tmp_path):
    missing = tmp_path / "nonexistent"
    # Should not raise
    remove_directories([missing])
    assert not missing.exists()


def test_remove_directories_skips_non_directories(tmp_path):
    file_path = tmp_path / "not-a-dir"
    file_path.write_text("keep", encoding="utf-8")

    remove_directories([file_path, tmp_path / "missing"])

    assert file_path.read_text(encoding="utf-8") == "keep"
