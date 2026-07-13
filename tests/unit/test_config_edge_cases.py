"""Edge-case tests for config parsing."""

import pytest
import yaml

from scrapeyard.common.yaml import MAX_YAML_NESTING, load_yaml_mapping
from scrapeyard.config.transforms import parse_transform
from scrapeyard.config.schema import PaginationConfig, SelectorLong, SelectorType


def test_parse_transform_join_func_syntax_raises():
    """join(",") should raise — it's a list-level operation, not a per-value transform."""
    with pytest.raises(ValueError, match="list-level operation"):
        parse_transform('join(",")')


def test_parse_transform_join_colon_syntax_raises():
    """join:, via colon syntax should also raise."""
    with pytest.raises(ValueError, match="list-level operation"):
        parse_transform("join:,")


def test_pagination_max_pages_zero():
    """max_pages=0 should be a valid PaginationConfig — doesn't raise."""
    cfg = PaginationConfig(next=".next-page", max_pages=0)
    assert cfg.max_pages == 0


def test_pagination_max_pages_default():
    """Default max_pages should be 10."""
    cfg = PaginationConfig(next=".next-page")
    assert cfg.max_pages == 10


def test_pagination_next_accepts_long_form_xpath_selector():
    """Pagination next selector can be long-form CSS/XPath config."""
    cfg = PaginationConfig(next={"query": "//a[contains(., 'Next')]", "type": "xpath"})

    assert isinstance(cfg.next, SelectorLong)
    assert cfg.next.query == "//a[contains(., 'Next')]"
    assert cfg.next.type == SelectorType.xpath


def _nested_mapping(collection_depth: int) -> str:
    lines = [f"{'  ' * depth}level_{depth}:" for depth in range(collection_depth)]
    lines.append(f"{'  ' * collection_depth}value: leaf")
    return "\n".join(lines)


def test_yaml_loader_accepts_collection_nesting_at_limit() -> None:
    parsed = load_yaml_mapping(_nested_mapping(MAX_YAML_NESTING - 1))

    assert "level_0" in parsed


def test_yaml_loader_rejects_one_collection_over_limit_without_recursion_error() -> None:
    with pytest.raises(
        yaml.YAMLError,
        match=rf"YAML nesting exceeds {MAX_YAML_NESTING} levels",
    ) as exc_info:
        load_yaml_mapping(_nested_mapping(MAX_YAML_NESTING))

    assert not isinstance(exc_info.value, RecursionError)
