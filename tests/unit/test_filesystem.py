"""Tests for scrapeyard.storage.filesystem — directory and JSON file helpers."""

import json
import os
from datetime import datetime

import pytest

from scrapeyard.common.json_encoding import iter_json_bytes
from scrapeyard.storage.filesystem import (
    FileSizeLimitExceeded,
    FilesystemDeadlineReached,
    ensure_directory,
    read_bytes_file_no_follow,
    read_json_file,
    read_json_file_no_follow,
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


def test_incremental_json_encoder_bounds_escaped_and_multibyte_byte_chunks():
    data = {"escaped": '"\\\n' * 100_000, "multibyte": "é" * 100_000}

    chunks = list(iter_json_bytes(data))

    assert max(map(len, chunks)) <= 16 * 1024
    assert b"".join(chunks) == json.dumps(
        data,
        default=str,
        separators=(",", ":"),
    ).encode("utf-8")


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


def test_write_json_file_fsyncs_file_and_parent_directory(tmp_path, monkeypatch):
    filepath = tmp_path / "data.json"
    fsynced: list[int] = []

    from scrapeyard.storage import filesystem

    real_fsync = filesystem.os.fsync

    def recording_fsync(fd: int) -> None:
        fsynced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(filesystem.os, "fsync", recording_fsync)

    write_json_file(filepath, {"durable": True})

    assert len(fsynced) == 2


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


def test_read_json_no_follow_rejects_symlink(tmp_path):
    target = tmp_path / "target.json"
    target.write_text('{"keep": true}', encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(target)

    with pytest.raises(OSError):
        read_json_file_no_follow(link)

    assert target.read_text(encoding="utf-8") == '{"keep": true}'


def test_read_bytes_no_follow_round_trips_and_rejects_symlink(tmp_path):
    target = tmp_path / "target.bin"
    target.write_bytes(b"preserve-me")
    link = tmp_path / "link.bin"
    link.symlink_to(target)

    assert read_bytes_file_no_follow(target) == b"preserve-me"
    with pytest.raises(OSError):
        read_bytes_file_no_follow(link)


def test_bounded_read_rejects_file_above_limit_without_returning_payload(tmp_path):
    target = tmp_path / "oversized.bin"
    target.write_bytes(b"12345")

    with pytest.raises(FileSizeLimitExceeded):
        read_bytes_file_no_follow(target, max_bytes=4)


def test_bounded_read_stops_file_that_grows_after_fstat(tmp_path, monkeypatch):
    target = tmp_path / "growing.bin"
    target.write_bytes(b"1234")
    from scrapeyard.storage import filesystem

    real_fstat = filesystem.os.fstat
    grown = False

    def grow_after_fstat(descriptor):
        nonlocal grown
        result = real_fstat(descriptor)
        if not grown:
            grown = True
            append_fd = os.open(target, os.O_WRONLY | os.O_APPEND)
            try:
                os.write(append_fd, b"5")
            finally:
                os.close(append_fd)
        return result

    monkeypatch.setattr(filesystem.os, "fstat", grow_after_fstat)
    monkeypatch.setattr(filesystem, "_READ_CHUNK_SIZE", 2)

    with pytest.raises(FileSizeLimitExceeded):
        read_bytes_file_no_follow(target, max_bytes=4)


def test_chunked_read_honors_cooperative_deadline(tmp_path, monkeypatch):
    target = tmp_path / "deadline.bin"
    target.write_bytes(b"12345")
    from scrapeyard.storage import filesystem

    monkeypatch.setattr(filesystem, "_READ_CHUNK_SIZE", 2)
    checks = 0

    def deadline_reached() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 3

    with pytest.raises(FilesystemDeadlineReached):
        read_bytes_file_no_follow(target, deadline_reached=deadline_reached)


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
