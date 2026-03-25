"""Tests for item-scoped extraction in scrape_target."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from scrapeyard.config.schema import (
    FetcherType,
    RetryConfig,
    StockDetectionConfig,
    StockPatternConfig,
    TargetConfig,
)
from scrapeyard.engine.scraper import scrape_target


class _Node:
    def __init__(self, *, text: str = "", css_map: dict[str, list[object]] | None = None) -> None:
        self.text = text
        self._css_map = css_map or {}
        self.attrib: dict[str, str] = {}

    def css(self, query: str) -> list[object]:
        return self._css_map.get(query, [])

    def xpath(self, query: str) -> list[object]:
        return []


@pytest.mark.asyncio
async def test_scrape_target_flattens_item_scoped_records(tmp_path):
    page = _Node(
        css_map={
            ".product-card": [
                _Node(
                    text="A $10 In Stock",
                    css_map={
                        ".title": [_Node(text="A")],
                        ".price": [_Node(text="$10")],
                        ".stock": [_Node(text="In Stock")],
                    },
                ),
                _Node(
                    text="B $20 In Stock",
                    css_map={
                        ".title": [_Node(text="B")],
                        ".price": [_Node(text="$20")],
                        ".stock": [_Node(text="In Stock")],
                    },
                ),
            ]
        }
    )
    page.status = 200

    target = TargetConfig(
        url="https://example.com/products",
        fetcher=FetcherType.basic,
        item_selector=".product-card",
        selectors={"name": ".title", "price": ".price", "stock_signal": ".stock"},
        stock_detection=StockDetectionConfig(
            in_stock=StockPatternConfig(text_patterns=["in stock"])
        ),
    )

    with patch("scrapeyard.engine.scraper.Fetcher") as mock_fetcher:
        mock_fetcher.get.return_value = page
        result = await scrape_target(
            target,
            adaptive=False,
            retry=RetryConfig(max_attempts=1),
            adaptive_dir=str(tmp_path),
        )

    assert result.status == "success"
    assert result.data == [
        {
            "name": "A",
            "price": "$10",
            "stock_signal": "In Stock",
            "pricing_visibility": "explicit",
            "display_price_text": None,
            "stock_status": "in_stock",
        },
        {
            "name": "B",
            "price": "$20",
            "stock_signal": "In Stock",
            "pricing_visibility": "explicit",
            "display_price_text": None,
            "stock_status": "in_stock",
        },
    ]
    assert result.pages_scraped == 1


@pytest.mark.asyncio
async def test_scrape_target_preserves_legacy_stock_status_selector_as_stock_signal(tmp_path):
    page = _Node(
        css_map={
            ".product-card": [
                _Node(
                    text="A $10 In Stock",
                    css_map={
                        ".title": [_Node(text="A")],
                        ".price": [_Node(text="$10")],
                        ".stock": [_Node(text="In Stock")],
                    },
                ),
            ]
        }
    )
    page.status = 200

    target = TargetConfig(
        url="https://example.com/products",
        fetcher=FetcherType.basic,
        item_selector=".product-card",
        selectors={"name": ".title", "price": ".price", "stock_status": ".stock"},
        stock_detection=StockDetectionConfig(
            in_stock=StockPatternConfig(text_patterns=["in stock"])
        ),
    )

    with patch("scrapeyard.engine.scraper.Fetcher") as mock_fetcher:
        mock_fetcher.get.return_value = page
        result = await scrape_target(
            target,
            adaptive=False,
            retry=RetryConfig(max_attempts=1),
            adaptive_dir=str(tmp_path),
        )

    assert result.status == "success"
    assert result.data == [
        {
            "name": "A",
            "price": "$10",
            "stock_signal": "In Stock",
            "pricing_visibility": "explicit",
            "display_price_text": None,
            "stock_status": "in_stock",
        },
    ]
    assert result.pages_scraped == 1
