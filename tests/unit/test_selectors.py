"""Tests for page-wide and item-scoped selector extraction."""

from __future__ import annotations

from scrapeyard.config.schema import SelectorLong, SelectorType
from scrapeyard.engine.selectors import (
    count_selector_matches,
    extract_selectors,
    select_items,
)


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


def test_select_items_returns_one_element_per_item() -> None:
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

    items = select_items(page, ".product-card")
    result = [extract_selectors(item, {"name": ".title", "price": ".price"}) for item in items]

    assert result == [
        {"name": "A", "price": "$10"},
        {"name": "B", "price": "$20"},
    ]


def test_select_items_supports_xpath_item_selector() -> None:
    item = _Node(css_map={".title": [_Node(text="Scoped Title")]})
    page = _Node(xpath_map={"//div[@class='product']": [item]})

    items = select_items(
        page,
        SelectorLong(query="//div[@class='product']", type=SelectorType.xpath),
    )
    result = [extract_selectors(node, {"title": ".title"}) for node in items]

    assert result == [{"title": "Scoped Title"}]


def test_extract_selectors_returns_none_for_invalid_selector_errors() -> None:
    class _ExplodingNode(_Node):
        def css(self, query: str) -> list[object]:
            raise ValueError(f"bad selector: {query}")

    result = extract_selectors(_ExplodingNode(), {"title": "[broken"})

    assert result == {"title": None}


def test_select_items_returns_empty_for_invalid_xpath_selector_errors() -> None:
    class _ExplodingNode(_Node):
        def xpath(self, query: str) -> list[object]:
            raise ValueError(f"bad xpath: {query}")

    items = select_items(
        _ExplodingNode(),
        SelectorLong(query="//*[", type=SelectorType.xpath),
    )

    assert items == []


def test_count_selector_matches_returns_zero_for_invalid_selector_errors() -> None:
    class _ExplodingNode(_Node):
        def css(self, query: str) -> list[object]:
            raise RuntimeError("selector engine failed")

    assert count_selector_matches(_ExplodingNode(), ".broken") == 0
