"""Edge-case tests for config parsing."""

import pytest

from scrapeyard.config.transforms import parse_transform
from scrapeyard.config.schema import PaginationConfig


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
