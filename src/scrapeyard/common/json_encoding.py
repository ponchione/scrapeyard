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
# Dict levels walked in Python by iter_json_bytes before one C-encoder call
# encodes the rest; list elements (such as records) are always encoded whole.
_ENCODE_WALK_DEPTH = 4
# Most consecutive list elements encoded by one C-encoder call.
_ENCODE_MAX_BATCH = 256
# Same output as iter_json_bytes' defaults; ASCII-only, so characters are bytes.
_SIZE_ENCODER = json.JSONEncoder(default=str, separators=(",", ":"))


def iter_json_bytes(
    data: Any,
    *,
    ensure_ascii: bool = True,
    allow_nan: bool = True,
    default: Callable[[Any], Any] | None = str,
) -> Iterator[bytes]:
    """Yield the compact JSON representation without building one whole string.

    ``JSONEncoder.iterencode`` walks the whole document in pure Python, so
    outer containers are walked here and each list element (such as one
    record) is encoded by the C encoder with the same options. The output is
    byte-identical, and chunks stay at most ``_JSON_TEXT_CHUNK_CHARS``
    characters.
    """

    encoder = json.JSONEncoder(
        ensure_ascii=ensure_ascii,
        allow_nan=allow_nan,
        default=default,
        separators=(",", ":"),
    )
    # Small pieces are joined into full chunks, and a large piece is sliced
    # without copying it whole again.
    pending: list[str] = []
    pending_chars = 0
    for text in _iter_json_text(data, encoder.encode, _ENCODE_WALK_DEPTH):
        if pending_chars + len(text) < _JSON_TEXT_CHUNK_CHARS:
            pending.append(text)
            pending_chars += len(text)
            continue
        offset = _JSON_TEXT_CHUNK_CHARS - pending_chars
        pending.append(text[:offset])
        yield "".join(pending).encode("utf-8")
        while len(text) - offset >= _JSON_TEXT_CHUNK_CHARS:
            yield text[offset : offset + _JSON_TEXT_CHUNK_CHARS].encode("utf-8")
            offset += _JSON_TEXT_CHUNK_CHARS
        pending = [text[offset:]]
        pending_chars = len(text) - offset
    if pending_chars:
        yield "".join(pending).encode("utf-8")


def _iter_json_text(value: Any, encode: Callable[[Any], str], depth: int) -> Iterator[str]:
    if depth and type(value) is list and value:
        separator = "["
        start = 0
        batch = 1
        while start < len(value):
            text = (
                encode(value[start])
                if batch == 1
                else encode(value[start : start + batch])[1:-1]
            )
            yield separator
            yield text
            separator = ","
            start += batch
            # Short elements are encoded in growing batches so each doesn't pay
            # for a C-encoder call. After long text it is one at a time again,
            # so long text isn't copied to drop a batch's brackets.
            batch = min(batch * 2, _ENCODE_MAX_BATCH) if len(text) < _JSON_TEXT_CHUNK_CHARS else 1
        yield "]"
    elif depth and type(value) is dict and value:
        separator = "{"
        for key, item in value.items():
            if type(key) is str and type(item) in (list, dict):
                yield separator + encode(key) + ":"
                yield from _iter_json_text(item, encode, depth - 1)
            else:
                # Let the encoder apply its key conversion rules.
                yield separator + encode({key: item})[1:-1]
            separator = ","
        yield "}"
    else:
        yield encode(value)


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
