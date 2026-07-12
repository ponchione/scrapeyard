"""Filesystem helpers for async storage code paths."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import uuid
from contextlib import suppress
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, TypeVar


T = TypeVar("T")


def ensure_directory(path: str | Path) -> None:
    """Create *path* if needed without deleting existing contents."""
    Path(path).mkdir(parents=True, exist_ok=True)


def serialize_json_bytes(data: Any) -> bytes:
    """Serialize using the exact compact representation persisted on disk."""
    return json.dumps(data, default=str, separators=(",", ":")).encode("utf-8")


def write_bytes_file(path: str | Path, payload: bytes) -> None:
    """Atomically write *payload* to *path* through a sibling temp file.

    A crash or ``ENOSPC`` mid-write leaves the target either unchanged or
    fully valid. The temporary file is removed on every exception path.
    """
    target = Path(path)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
        directory_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        with suppress(FileNotFoundError):
            tmp.unlink()


def write_json_file(path: str | Path, data: Any) -> None:
    """Atomically serialize compact JSON to *path*."""
    write_bytes_file(path, serialize_json_bytes(data))


async def cleanup_safe_to_thread(func: Callable[..., T], *args: Any) -> T:
    """Finish a blocking filesystem operation before propagating cancellation."""
    task = asyncio.create_task(asyncio.to_thread(func, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with suppress(Exception):
            await task
        raise


def read_json_file(path: str | Path) -> Any:
    """Load JSON data from *path*."""
    target = Path(path)
    return json.loads(target.read_text(encoding="utf-8"))


def read_json_file_no_follow(path: str | Path) -> Any:
    """Load a regular JSON file without following its final symlink component."""

    target = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags)
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("JSON artifact is not a regular file")
        with os.fdopen(descriptor, "r", encoding="utf-8") as fh:
            descriptor = -1
            return json.load(fh)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def remove_directories(paths: Iterable[str | Path]) -> None:
    """Recursively remove any directories in *paths* that still exist."""
    for path in paths:
        target = Path(path)
        if target.is_symlink() or not target.is_dir():
            continue
        with suppress(FileNotFoundError):
            shutil.rmtree(target)
