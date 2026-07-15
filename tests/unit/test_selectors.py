"""Tests for page-wide and item-scoped selector extraction."""

from __future__ import annotations

import json
import random
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from scrapling import Adaptor

from scrapeyard.common.budgets import BudgetExceeded, BudgetLimitName, RunBudget
from scrapeyard.config.schema import (
    MAX_SELECTORS_PER_TARGET,
    MAX_SELECTOR_FIELD_NAME_CHARS,
    MAX_SELECTOR_QUERY_CHARS,
    SelectorLong,
    SelectorType,
    TargetConfig,
)
from scrapeyard.engine.scraper import _extract_page_data
from scrapeyard.engine.selectors import (
    SelectorExecutionError,
    _json_string_serialized_size,
    count_selector_matches_strict,
    extract_selectors_strict,
    select_items_strict,
)


@pytest.mark.parametrize(
    "value",
    [
        "\x1f",
        " ",
        '"',
        "\\",
        "~",
        "\x7f",
        "\x80",
        "\uffff",
        "\U00010000",
        "\U0001f600",
        "\ud800",
        "\udfff",
    ],
)
def test_json_string_size_matches_ascii_json_boundaries(value: str) -> None:
    expected = len(json.dumps(value, separators=(",", ":")).encode("utf-8"))

    assert _json_string_serialized_size(value) == expected


def test_json_string_size_matches_deterministic_random_values() -> None:
    randomizer = random.Random(20260714)
    codepoints = [
        0,
        0x1F,
        0x20,
        ord('"'),
        ord("\\"),
        0x7E,
        0x7F,
        0x80,
        0xD800,
        0xDFFF,
        0xFFFF,
        0x10000,
        0x10FFFF,
    ]
    codepoints.extend(randomizer.randrange(0x110000) for _ in range(500))
    value = "".join(chr(codepoint) for codepoint in codepoints)

    assert _json_string_serialized_size(value) == len(
        json.dumps(value, separators=(",", ":")).encode("utf-8")
    )


def test_del_heavy_extraction_exhausts_budget_during_estimation() -> None:
    budget = _value_budget(30)
    page = _Node(css_map={"h1": [_Node(text="\x7f" * 5)]})

    with pytest.raises(BudgetExceeded) as raised:
        extract_selectors_strict(
            page,
            {"title": "h1"},
            reserve_output_bytes=budget.reserve_estimated_result_bytes,
        )

    assert raised.value.limit_name is BudgetLimitName.serialized_result_bytes


def _value_budget(max_serialized_result_bytes: int) -> RunBudget:
    return RunBudget(
        max_duration_seconds=60,
        max_fetched_bytes=10_000,
        max_extracted_records=100,
        max_serialized_result_bytes=max_serialized_result_bytes,
        max_browser_debug_bytes=1_000,
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


def _adaptor(html: str) -> Adaptor:
    return Adaptor(html)


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


@pytest.mark.parametrize(
    ("markup", "expected"),
    [
        ("Direct only", "Direct only"),
        ("<span>Descendant only</span>", "Descendant only"),
        ("Before <span>descendant</span>", "Before descendant"),
        ("<span>SKU 123</span> In Stock", "SKU 123 In Stock"),
        (
            "Alpha <span>Beta <em>Gamma</em> Delta</span> Omega",
            "Alpha Beta Gamma Delta Omega",
        ),
        ("None", "None"),
    ],
)
def test_extract_selectors_reads_complete_real_adaptor_text(
    markup: str,
    expected: str,
) -> None:
    page = _adaptor(f'<div class="product">{markup}</div>')

    result = extract_selectors_strict(page, {"text": ".product"})

    assert result == {"text": expected}


def test_extract_selectors_excludes_non_visible_script_and_style_text() -> None:
    page = _adaptor(
        '<div class="product">Before<script>ignored()</script> Middle'
        "<style>.ignored {}</style> After</div>"
    )

    result = extract_selectors_strict(page, {"text": ".product"})

    assert result == {"text": "Before Middle After"}


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


@pytest.mark.parametrize("transform", [None, "trim"])
def test_selector_value_limit_applies_with_and_without_transform(
    monkeypatch,
    transform,
) -> None:
    monkeypatch.setattr(
        "scrapeyard.config.transforms.get_settings",
        lambda: type("Settings", (), {"transform_max_value_bytes": 1024})(),
    )
    selector = "h1" if transform is None else SelectorLong(query="h1", transform=transform)
    page = _Node(css_map={"h1": [_Node(text="x" * 1025)]})

    with pytest.raises(ValueError, match="Selector value exceeds 1024 UTF-8 bytes"):
        extract_selectors_strict(page, {"title": selector})


@pytest.mark.parametrize("transform", [None, "trim"])
def test_selector_value_limit_accepts_exact_multibyte_boundary(
    monkeypatch,
    transform,
) -> None:
    monkeypatch.setattr(
        "scrapeyard.config.transforms.get_settings",
        lambda: type("Settings", (), {"transform_max_value_bytes": 1024})(),
    )
    selector = "h1" if transform is None else SelectorLong(query="h1", transform=transform)
    page = _Node(css_map={"h1": [_Node(text="é" * 512)]})

    assert extract_selectors_strict(page, {"title": selector}) == {"title": "é" * 512}


def test_selector_value_limit_measures_multibyte_input_in_bytes(monkeypatch) -> None:
    monkeypatch.setattr(
        "scrapeyard.config.transforms.get_settings",
        lambda: type("Settings", (), {"transform_max_value_bytes": 1024})(),
    )
    page = _Node(css_map={"h1": [_Node(text="é" * 513)]})

    with pytest.raises(ValueError, match="Selector value exceeds 1024 UTF-8 bytes"):
        extract_selectors_strict(page, {"title": "h1"})


def test_aggregate_result_budget_rejects_legal_list_values_during_extraction() -> None:
    page = _Node(css_map={".value": [_Node(text="x" * 800) for _ in range(6)]})
    budget = _value_budget(4096)

    with pytest.raises(BudgetExceeded) as exc_info:
        extract_selectors_strict(
            page,
            {"values": ".value"},
            reserve_output_bytes=budget.reserve_estimated_result_bytes,
        )

    assert exc_info.value.limit_name is BudgetLimitName.serialized_result_bytes
    assert budget.estimated_result_bytes < budget.max_serialized_result_bytes


def test_many_selectors_cannot_repeat_one_large_page_value_past_budget() -> None:
    selectors = {f"field_{index}": "h1" for index in range(6)}
    target = TargetConfig.model_construct(
        url="https://example.com",
        selectors=selectors,
        item_selector=None,
        map_detection=None,
        stock_detection=None,
    )
    page = _Node(css_map={"h1": [_Node(text="x" * 800)]})
    budget = _value_budget(4096)

    with pytest.raises(BudgetExceeded) as exc_info:
        _extract_page_data(page, target, budget=budget)

    assert exc_info.value.limit_name is BudgetLimitName.serialized_result_bytes


def test_first_page_reserves_all_matches_before_extracting_any_row() -> None:
    items = [_Node(), _Node()]
    page = _Node(css_map={".item": items})
    target = TargetConfig.model_validate(
        {
            "url": "https://example.com",
            "item_selector": ".item",
            "selectors": {"title": "h1"},
        }
    )
    budget = _value_budget(4096)
    budget.max_extracted_records = 1

    with patch(
        "scrapeyard.engine.scraper.extract_selectors_strict"
    ) as extract_selectors:
        with pytest.raises(BudgetExceeded) as exc_info:
            _extract_page_data(page, target, budget=budget)

    assert exc_info.value.limit_name is BudgetLimitName.extracted_records
    assert exc_info.value.observed_amount == 2
    assert budget.extracted_records == 0
    extract_selectors.assert_not_called()


def test_first_page_accepts_exact_record_boundary() -> None:
    items = [_Node(), _Node()]
    page = _Node(css_map={".item": items})
    target = TargetConfig.model_validate(
        {
            "url": "https://example.com",
            "item_selector": ".item",
            "selectors": {"title": "h1"},
        }
    )
    budget = _value_budget(4096)
    budget.max_extracted_records = 2

    rows = _extract_page_data(page, target, budget=budget)

    assert len(rows) == 2
    assert budget.extracted_records == 2


def test_concurrent_page_reservations_never_extract_past_aggregate_limit() -> None:
    page = _Node(css_map={".item": [_Node(), _Node()]})
    target = TargetConfig.model_validate(
        {
            "url": "https://example.com",
            "item_selector": ".item",
            "selectors": {"title": "h1"},
        }
    )
    budget = _value_budget(4096)
    budget.max_extracted_records = 2

    with patch(
        "scrapeyard.engine.scraper.extract_selectors_strict",
        return_value={"title": None},
    ) as extract_selectors:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(_extract_page_data, page, target, budget=budget)
                for _index in range(2)
            ]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result())
            except BudgetExceeded as exc:
                outcomes.append(exc)

    assert budget.extracted_records == 2
    assert extract_selectors.call_count == 2
    assert sum(isinstance(outcome, BudgetExceeded) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, list) for outcome in outcomes) == 1


def test_detection_overhead_crosses_result_ceiling_before_row_is_retained() -> None:
    target = TargetConfig.model_validate(
        {
            "url": "https://example.com",
            "selectors": {"missing": ".missing"},
        }
    )
    budget = _value_budget(97)

    with pytest.raises(BudgetExceeded) as exc_info:
        _extract_page_data(_Node(), target, budget=budget)

    assert exc_info.value.limit_name is BudgetLimitName.serialized_result_bytes
    assert exc_info.value.observed_amount == 99
    assert budget.estimated_result_bytes < budget.max_serialized_result_bytes


def test_aggregate_result_budget_accepts_exact_estimated_boundary() -> None:
    page = _Node(css_map={"h1": [_Node(text="a")]})
    budget = _value_budget(9)  # compact JSON: {"x":"a"}

    assert extract_selectors_strict(
        page,
        {"x": "h1"},
        reserve_output_bytes=budget.reserve_estimated_result_bytes,
    ) == {"x": "a"}
    assert budget.estimated_result_bytes == 9


def test_target_selector_schema_limits_count_names_and_queries() -> None:
    base = {"url": "https://example.com"}
    exact = {
        "f" * MAX_SELECTOR_FIELD_NAME_CHARS: "q" * MAX_SELECTOR_QUERY_CHARS,
        **{f"field-{index}": "h1" for index in range(MAX_SELECTORS_PER_TARGET - 1)},
    }
    assert len(TargetConfig.model_validate({**base, "selectors": exact}).selectors) == 100

    invalid_values = [
        {f"field-{index}": "h1" for index in range(MAX_SELECTORS_PER_TARGET + 1)},
        {"f" * (MAX_SELECTOR_FIELD_NAME_CHARS + 1): "h1"},
        {"title": "q" * (MAX_SELECTOR_QUERY_CHARS + 1)},
    ]
    for selectors in invalid_values:
        with pytest.raises(ValidationError):
            TargetConfig.model_validate({**base, "selectors": selectors})
