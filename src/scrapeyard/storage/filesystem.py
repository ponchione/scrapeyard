"""Filesystem helpers for async storage code paths."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def prepare_directory(path: str | Path) -> None:
    """Recreate *path* as an empty directory."""
    target = Path(path)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)


def write_json_file(path: str | Path, data: Any) -> None:
    """Atomically serialize *data* to compact JSON at *path*.

    Writes to a sibling ``*.tmp`` file, ``fsync``s it, then ``os.replace``s
    onto the final name. A crash or ``ENOSPC`` mid-write leaves the target
    path either unchanged or fully valid — never truncated.
    """
    target = Path(path)
    payload = json.dumps(data, default=str, separators=(",", ":"))
    tmp = target.with_name(target.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, target)


def read_json_file(path: str | Path) -> Any:
    """Load JSON data from *path*."""
    target = Path(path)
    return json.loads(target.read_text(encoding="utf-8"))


def remove_directories(paths: Iterable[str | Path]) -> None:
    """Recursively remove any directories in *paths* that still exist."""
    for path in paths:
        target = Path(path)
        if target.exists():
            shutil.rmtree(target)
