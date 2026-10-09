from __future__ import annotations

import datetime
import enum
import json
import random
from collections import OrderedDict
from typing import Any

import pytest

from scrapeyard.common.json_encoding import compact_json_size, iter_json_bytes


class _Color(str, enum.Enum):
    red = "red"


def _streamed_size(data: Any) -> int:
    return sum(len(chunk) for chunk in iter_json_bytes(data))


def _random_value(rng: random.Random, depth: int) -> Any:
    roll = rng.random()
    if depth <= 0 or roll < 0.45:
        return rng.choice([
            "", "plain", 'q"uote\\', "ünïcödé", "😀", "\x00\x1f\x7f", 0, -7, 2**70, 1.5,
            float("nan"), float("inf"), True, None, _Color.red, datetime.date(2026, 9, 25),
            (1, "t"),
        ])
    if roll < 0.7:
        return [_random_value(rng, depth - 1) for _ in range(rng.randrange(5))]
    mapping: dict[Any, Any] = {} if rng.random() < 0.8 else OrderedDict()
    for _ in range(rng.randrange(5)):
        key = rng.choice(["a", "b", 'k"e\\y', "ключ", 1, 2.5, True, None])
        mapping[key] = _random_value(rng, depth - 1)
    return mapping


@pytest.mark.parametrize("seed", range(3))
def test_compact_json_size_matches_the_streamed_encoding(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(1000):
        value = _random_value(rng, rng.randrange(7))
        assert compact_json_size(value) == _streamed_size(value)


def test_compact_json_size_walks_merged_and_grouped_results() -> None:
    records = [{"name": f"Item {n}", "tags": ["a", "b"], "price": None} for n in range(300)]
    merged = {"status": "complete", "results": records}
    grouped = {"status": "complete", "results": {"www.example.test": {"data": records}}}

    assert compact_json_size(merged) == _streamed_size(merged)
    assert compact_json_size(grouped) == _streamed_size(grouped)


def test_compact_json_size_rejects_what_the_encoder_rejects() -> None:
    cycle: list[Any] = []
    cycle.append(cycle)
    with pytest.raises(ValueError, match="Circular reference"):
        compact_json_size({"results": [cycle]})
    with pytest.raises(TypeError, match="keys must be"):
        compact_json_size({"results": [{(1, 2): "tuple key"}]})


# json's message for NaN or infinity with allow_nan=False.
_NON_FINITE_MESSAGE = "Out of range float values are not JSON compliant"

# The persisted artifact options, and the stricter /results response options.
_OPTION_SETS = [
    pytest.param({}, id="artifact"),
    pytest.param({"ensure_ascii": False, "allow_nan": False, "default": None}, id="response"),
]


def _reference_json_bytes(data: Any, **options: Any) -> bytes:
    """Encode with the pure-Python walk of JSONEncoder.iterencode."""
    encoder = json.JSONEncoder(separators=(",", ":"), **{"default": str, **options})
    return b"".join(chunk.encode("utf-8") for chunk in encoder.iterencode(data))


def _outcome(encode: Any) -> tuple[Any, ...]:
    try:
        return ("bytes", encode())
    except Exception as exc:
        message = str(exc)
        # On Python 3.10 the C encoder leaves the value out of this message.
        if message.startswith(_NON_FINITE_MESSAGE):
            message = _NON_FINITE_MESSAGE
        return (type(exc), message)


def _assert_matches_reference(data: Any, options: dict[str, Any]) -> None:
    assert _outcome(lambda: b"".join(iter_json_bytes(data, **options))) == _outcome(
        lambda: _reference_json_bytes(data, **options)
    )


def _envelope(rng: random.Random, *, grouped: bool) -> dict[str, Any]:
    records = [
        {
            "name": rng.choice(["Scope", "ünïcödé", 'q"uote\\', "😀", "x" * 20_000]),
            "price": rng.choice([None, "$1.00", 12.5, 3]),
            "tags": [rng.choice(["a", "b"]) for _ in range(rng.randrange(3))],
            "extra": _random_value(rng, rng.randrange(4)),
            "_source": "www.example.test",
        }
        for _ in range(rng.randrange(1, 400))
    ]
    return {
        "job_id": "job-1",
        "status": "complete",
        "errors": [],
        "targets": [{"url": "https://www.example.test/", "count": len(records), "debug": {}}],
        "budget_error": None,
        "results": {"www.example.test": {"data": records}} if grouped else records,
    }


@pytest.mark.parametrize("options", _OPTION_SETS)
@pytest.mark.parametrize("seed", range(3))
def test_iter_json_bytes_matches_the_pure_python_encoder(seed: int, options: dict[str, Any]) -> None:
    rng = random.Random(seed)
    for _ in range(1000):
        _assert_matches_reference(_random_value(rng, rng.randrange(7)), options)
    for _ in range(20):
        _assert_matches_reference(_envelope(rng, grouped=rng.random() < 0.5), options)


@pytest.mark.parametrize("options", _OPTION_SETS)
def test_iter_json_bytes_matches_the_pure_python_encoder_on_edge_cases(
    options: dict[str, Any],
) -> None:
    cyclic_dict: dict[str, Any] = {}
    cyclic_dict["self"] = cyclic_dict
    cyclic_list: list[Any] = []
    cyclic_list.append(cyclic_list)
    cyclic_record: dict[str, Any] = {"results": [{"name": "a"}]}
    cyclic_record["results"][0]["envelope"] = cyclic_record
    deep: Any = "leaf"
    for _ in range(50):
        deep = {"nested": [deep]}

    for data in [
        {},
        [],
        "",
        {"results": []},
        {"results": {}},
        {"results": [[], {}, [[]], [{}]]},
        {2: "int", 2.5: "float", True: "bool", False: "bool", None: "none", "s": [1]},
        {float("nan"): "nan key", "results": [1]},
        {"results": [float("nan"), float("inf"), -0.0, 2**70]},
        {"results": [datetime.date(2026, 10, 9)]},
        {"results": [{(1, 2): "tuple key"}]},
        {"results": list(range(5000))},
        {"results": ["é" * 40_000, "x"]},
        cyclic_dict,
        cyclic_list,
        cyclic_record,
        deep,
    ]:
        _assert_matches_reference(data, options)


@pytest.mark.parametrize("options", _OPTION_SETS)
def test_iter_json_bytes_chunks_stay_bounded(options: dict[str, Any]) -> None:
    data = {
        "results": [{"name": "é" * 50_000}, *({"name": str(n)} for n in range(10_000))],
        "errors": ['"\\\n' * 30_000],
    }

    chunks = list(iter_json_bytes(data, **options))

    assert len(chunks) > 1
    for chunk in chunks:
        # Chunks split on characters, so each is valid UTF-8 on its own.
        assert len(chunk.decode("utf-8")) <= 16 * 1024


def test_iter_json_bytes_rejects_lone_surrogates_without_ascii_escaping() -> None:
    data = {"results": [{"name": "\ud800"}]}

    assert b"".join(iter_json_bytes(data)) == _reference_json_bytes(data)
    with pytest.raises(UnicodeEncodeError):
        b"".join(iter_json_bytes(data, ensure_ascii=False))
