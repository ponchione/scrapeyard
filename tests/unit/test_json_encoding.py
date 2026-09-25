from __future__ import annotations

import datetime
import enum
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
