"""Tests for scrapeyard.storage.filesystem — directory and JSON file helpers."""

from datetime import datetime

import pytest

from scrapeyard.storage.filesystem import (
    prepare_directory,
    read_json_file,
    remove_directories,
    write_json_file,
)


def test_prepare_directory_creates_empty_dir(tmp_path):
    target = tmp_path / "new_dir"
    prepare_directory(target)
    assert target.is_dir()
    assert list(target.iterdir()) == []


def test_prepare_directory_clears_existing_contents(tmp_path):
    target = tmp_path / "existing"
    target.mkdir()
    (target / "old_file.txt").write_text("stale")
    (target / "subdir").mkdir()
    (target / "subdir" / "nested.txt").write_text("nested")

    prepare_directory(target)

    assert target.is_dir()
    assert list(target.iterdir()) == []


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
