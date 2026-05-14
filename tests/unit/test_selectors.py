"""Tests for page-wide and item-scoped selector extraction."""

from __future__ import annotations

import pytest

from scrapeyard.config.schema import SelectorLong, SelectorType
from scrapeyard.engine.selectors import (
    SelectorExecutionError,
    count_selector_matches_strict,
    extract_selectors_strict,
    select_items_strict,
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


class _NestedTextNode(_Node):
    def __init__(self, *, text: str = "", all_text: str) -> None:
        super().__init__(text=text)
        self._all_text = all_text

    def get_all_text(self) -> str:
        return self._all_text


def test_extract_selectors_page_wide_scalar_and_list() -> None:
    page = _Node(
        css_map={
            "h1": [_Node(text="Title")],
            ".price": [_Node(text="$10"), _Node(text="$20")],
        }
    )

    result = extract_selectors_strict(page, {"title": "h1", "prices": ".price"})

    assert result == {"title": "Title", "prices": ["$10", "$20"]}


def test_extract_selectors_reads_descendant_text_when_direct_text_is_empty() -> None:
    page = _Node(css_map={".title": [_NestedTextNode(text="", all_text="Nested Title")]})

    result = extract_selectors_strict(page, {"title": ".title"})

    assert result == {"title": "Nested Title"}


def test_extract_selectors_combines_direct_and_descendant_text() -> None:
    page = _Node(css_map={".title": [_NestedTextNode(text="Title", all_text="Suffix")]})

    result = extract_selectors_strict(page, {"title": ".title"})

    assert result == {"title": "Title\nSuffix"}


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

    items = select_items_strict(page, ".product-card")
    result = [extract_selectors_strict(item, {"name": ".title", "price": ".price"}) for item in items]

    assert result == [
        {"name": "A", "price": "$10"},
        {"name": "B", "price": "$20"},
    ]


def test_select_items_supports_xpath_item_selector() -> None:
    item = _Node(css_map={".title": [_Node(text="Scoped Title")]})
    page = _Node(xpath_map={"//div[@class='product']": [item]})

    items = select_items_strict(
        page,
        SelectorLong(query="//div[@class='product']", type=SelectorType.xpath),
    )
    result = [extract_selectors_strict(node, {"title": ".title"}) for node in items]

    assert result == [{"title": "Scoped Title"}]


def test_extract_selectors_strict_raises_selector_execution_error_with_field_metadata() -> None:
    class _ExplodingNode(_Node):
        def css(self, query: str) -> list[object]:
            raise ValueError(f"bad selector: {query}")

    with pytest.raises(SelectorExecutionError) as exc_info:
        extract_selectors_strict(_ExplodingNode(), {"title": "[broken"})

    err = exc_info.value
    assert err.operation == "extract_selectors"
    assert err.field_name == "title"
    assert err.query == "[broken"
    assert err.debug["exception_type"] == "ValueError"


def test_strict_selector_helpers_raise_same_selector_execution_error_shape() -> None:
    class _ExplodingNode(_Node):
        def xpath(self, query: str) -> list[object]:
            raise RuntimeError(f"bad xpath: {query}")

    selector = SelectorLong(query="//*[", type=SelectorType.xpath)

    with pytest.raises(SelectorExecutionError) as select_exc:
        select_items_strict(_ExplodingNode(), selector)
    assert select_exc.value.operation == "select_items"
    assert select_exc.value.field_name is None

    with pytest.raises(SelectorExecutionError) as count_exc:
        count_selector_matches_strict(_ExplodingNode(), selector, field_name="price")
    assert count_exc.value.operation == "count_selector_matches"
    assert count_exc.value.field_name == "price"
