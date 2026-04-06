"""Filesystem helpers for async storage code paths."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Iterable


def prepare_directory(path: str | Path) -> None:
    """Recreate *path* as an empty directory."""
    target = Path(path)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)


def write_json_file(path: str | Path, data: Any) -> None:
    """Serialize *data* to compact JSON at *path*."""
    target = Path(path)
    target.write_text(
        json.dumps(data, default=str, separators=(",", ":")),
        encoding="utf-8",
    )


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
