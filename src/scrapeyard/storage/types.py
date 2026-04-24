"""Shared storage-layer data types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ResultPayload:
    """Wrapper returned by get_result with run context."""

    run_id: str
    data: Any


@dataclass(frozen=True, slots=True)
class SaveResultMeta:
    """Metadata returned from a save_result call."""

    run_id: str
    file_path: str
    record_count: int | None
