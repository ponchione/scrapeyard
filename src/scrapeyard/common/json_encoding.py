"""Shared incremental JSON encoding helpers."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from json.encoder import encode_basestring_ascii
from typing import Any

_JSON_TEXT_CHUNK_CHARS = 16 * 1024
# Container levels walked in Python by compact_json_size before one C-encoder
# call sizes the rest: enough to reach records in merged and grouped results.
_SIZE_WALK_DEPTH = 4
# Same output as iter_json_bytes' defaults; ASCII-only, so characters are bytes.
_SIZE_ENCODER = json.JSONEncoder(default=str, separators=(",", ":"))


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
    """Return the persisted compact JSON byte size without retaining its bytes.

    Outer containers are walked here and each inner value (such as one record)
    is sized by the C encoder, so no string of the whole document is built.
    """

    return _compact_json_size(data, _SIZE_WALK_DEPTH)


def _compact_json_size(value: Any, depth: int) -> int:
    if depth and type(value) is list and value:
        # "[" + items joined by "," + "]"
        return len(value) + 1 + sum(_compact_json_size(item, depth - 1) for item in value)
    if depth and type(value) is dict and value and any(
        type(item) in (list, dict) for item in value.values()
    ):
        # "{" + '"key":value' pairs joined by "," + "}"
        size = len(value) + 1
        for key, item in value.items():
            if type(key) is str:
                size += len(encode_basestring_ascii(key)) + 1
                size += _compact_json_size(item, depth - 1)
            else:
                # Let the encoder apply its key conversion rules.
                size += len(_SIZE_ENCODER.encode({key: item})) - 2
        return size
    return len(_SIZE_ENCODER.encode(value))


def compact_json_bytes(data: Any) -> bytes:
    """Build the persisted compact JSON representation when bytes are required."""

    return b"".join(iter_json_bytes(data))
