"""Shared incremental JSON encoding helpers."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

_JSON_TEXT_CHUNK_CHARS = 16 * 1024


def iter_json_bytes(
    data: Any,
    *,
    ensure_ascii: bool = True,
    allow_nan: bool = True,
    default: Callable[[Any], Any] | None = str,
) -> Iterator[bytes]:
    """Yield the compact JSON representation without building one whole string."""

    encoder = json.JSONEncoder(
        ensure_ascii=ensure_ascii,
        allow_nan=allow_nan,
        default=default,
        separators=(",", ":"),
    )
    for chunk in encoder.iterencode(data):
        for start in range(0, len(chunk), _JSON_TEXT_CHUNK_CHARS):
            yield chunk[start : start + _JSON_TEXT_CHUNK_CHARS].encode("utf-8")


def compact_json_size(data: Any) -> int:
    """Return the persisted compact JSON byte size without retaining its bytes."""

    return sum(len(chunk) for chunk in iter_json_bytes(data))


def compact_json_bytes(data: Any) -> bytes:
    """Build the persisted compact JSON representation when bytes are required."""

    return b"".join(iter_json_bytes(data))
