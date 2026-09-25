from __future__ import annotations

import random
import re
from unittest.mock import MagicMock

import pytest
from scrapling.engines.toolbelt.custom import Response

from scrapeyard.engine.browser_debug import populate_fetch_debug, truncate_text


def _reference_truncate(value: str, limit: int) -> str:
    normalized = re.sub(r"\s+", " ", value).strip()
    return normalized if len(normalized) <= limit else normalized[: limit - 3] + "..."


@pytest.mark.parametrize("seed", range(4))
def test_truncate_text_matches_normalizing_the_whole_value(seed: int) -> None:
    rng = random.Random(seed)
    pieces = ["word", " ", "\n", "\t", " ", "　", "é", "  \n  "]
    for _ in range(500):
        limit = rng.choice([0, 1, 2, 3, 4, 10, 300, 2000])
        length = rng.choice(
            [limit, limit + 1, limit + 2, 4 * limit, 4 * limit + 1, rng.randrange(20 * limit + 5)]
        )
        weights = [rng.random() for _ in pieces]
        value = "".join(rng.choices(pieces, weights=weights, k=length))
        assert truncate_text(value, limit) == _reference_truncate(value, limit)


def test_basic_page_debug_serializes_the_document_once(monkeypatch: pytest.MonkeyPatch) -> None:
    body = "<html><head><title> Catalog  page </title></head><body>" + "<p>item</p>\n" * 5000
    response = Response(
        url="https://www.example.test/", text=body, body=body.encode(), status=200,
        reason="OK", cookies={}, headers={}, request_headers={}, encoding="utf-8",
    )
    serialized = MagicMock(side_effect=lambda: Response.html_content.fget(response))
    monkeypatch.setattr(Response, "body", property(lambda _self: serialized()))

    debug: dict[str, object] = {}
    populate_fetch_debug(debug, response, "https://www.example.test/")

    assert serialized.call_count == 1
    assert debug["page_title"] == "Catalog page"
    excerpt = str(debug["html_excerpt"])
    assert len(excerpt) == 2000 and excerpt.endswith("...")
    assert excerpt == _reference_truncate(Response.html_content.fget(response), 2000)
