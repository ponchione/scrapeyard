"""Tests for page-wide and item-scoped selector extraction."""

from __future__ import annotations

from scrapeyard.config.schema import SelectorLong, SelectorType
from scrapeyard.engine.selectors import extract_item_selectors, extract_selectors


class _Node:
    def __init__(
        self,
        *,
        text: str = "",
        css_map: dict[str, list[object]] | None = None,
        xpath_map: dict[str, list[object]] | None = None,
    ) -> None:
        self.text = text
        self._css_map = css_map or {}
        self._xpath_map = xpath_map or {}

    def css(self, query: str) -> list[object]:
        return self._css_map.get(query, [])

    def xpath(self, query: str) -> list[object]:
        return self._xpath_map.get(query, [])


def test_extract_selectors_page_wide_scalar_and_list() -> None:
    page = _Node(
        css_map={
            "h1": [_Node(text="Title")],
            ".price": [_Node(text="$10"), _Node(text="$20")],
        }
    )

    result = extract_selectors(page, {"title": "h1", "prices": ".price"})

    assert result == {"title": "Title", "prices": ["$10", "$20"]}


def test_extract_item_selectors_returns_one_record_per_item() -> None:
    item_one = _Node(
        css_map={
            ".title": [_Node(text="A")],
            ".price": [_Node(text="$10")],
        }
    )
    item_two = _Node(
        css_map={
            ".title": [_Node(text="B")],
            ".price": [_Node(text="$20")],
        }
    )
    page = _Node(css_map={".product-card": [item_one, item_two]})

    result = extract_item_selectors(
        page,
        ".product-card",
        {"name": ".title", "price": ".price"},
    )

    assert result == [
        {"name": "A", "price": "$10"},
        {"name": "B", "price": "$20"},
    ]


def test_extract_item_selectors_supports_xpath_item_selector() -> None:
    item = _Node(css_map={".title": [_Node(text="Scoped Title")]})
    page = _Node(xpath_map={"//div[@class='product']": [item]})

    result = extract_item_selectors(
        page,
        SelectorLong(query="//div[@class='product']", type=SelectorType.xpath),
        {"title": ".title"},
    )

    assert result == [{"title": "Scoped Title"}]
