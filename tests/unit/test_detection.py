"""Unit tests for MAP detection and stock status classification."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from scrapling import Adaptor

from scrapeyard.config.schema import MapDetectionConfig, StockDetectionConfig, StockPatternConfig
from scrapeyard.engine.detection import (
    detect_pricing_visibility,
    detect_stock_status,
    enrich_item_detection,
)


def _mock_element(text: str = "", css_results: dict[str, list] | None = None):
    """Create a mock DOM element with .text and .css() support."""
    el = MagicMock()
    el.text = text
    _css = css_results or {}

    def _css_side_effect(selector: str):
        return _css.get(selector, [])

    el.css.side_effect = _css_side_effect
    return el


class TestDetectPricingVisibilityExplicit:
    """Listings with a numeric price -> always explicit."""

    def test_integer_price_returns_explicit(self):
        item = {"price": "29999"}
        vis, text = detect_pricing_visibility(item, _mock_element(), None)
        assert vis == "explicit"
        assert text is None

    def test_dollar_price_returns_explicit(self):
        item = {"price": "$299.99"}
        vis, text = detect_pricing_visibility(item, _mock_element(), None)
        assert vis == "explicit"
        assert text is None

    def test_price_with_commas_returns_explicit(self):
        item = {"price": "$1,299.99"}
        vis, text = detect_pricing_visibility(item, _mock_element(), None)
        assert vis == "explicit"
        assert text is None

    def test_explicit_overrides_map_detection_config(self):
        """Numeric price -> explicit regardless of MAP config."""
        config = MapDetectionConfig(
            text_patterns=["add to cart to see price"],
            css_selectors=[".map-message"],
        )
        item = {"price": "$299.99"}
        el = _mock_element(
            text="Add to Cart to See Price $299.99",
            css_results={".map-message": [_mock_element("MAP")]},
        )
        vis, text = detect_pricing_visibility(item, el, config)
        assert vis == "explicit"
        assert text is None

    def test_zero_string_price_with_map_detection_is_explicit(self):
        config = MapDetectionConfig(text_patterns=["add to cart to see price"])
        item = {"price": "0"}
        el = _mock_element(text="Add to Cart to See Price")
        vis, text = detect_pricing_visibility(item, el, config)
        assert vis == "explicit"
        assert text is None

    def test_raw_numeric_zero_returns_explicit(self):
        item = {"price": 0}
        vis, text = detect_pricing_visibility(item, _mock_element(), None)
        assert vis == "explicit"
        assert text is None

    def test_raw_numeric_zero_point_zero_returns_explicit(self):
        item = {"price": 0.0}
        vis, text = detect_pricing_visibility(item, _mock_element(), None)
        assert vis == "explicit"
        assert text is None

    @pytest.mark.parametrize("price", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_raw_float_prices_are_not_explicit(self, price):
        item = {"price": price}
        vis, text = detect_pricing_visibility(item, _mock_element(), None)
        assert vis == "unknown"
        assert text is None


class TestDetectPricingVisibilityUnknown:
    """No price + no map_detection config -> unknown."""

    def test_no_price_no_config_returns_unknown(self):
        item = {"price": None}
        vis, text = detect_pricing_visibility(item, _mock_element(), None)
        assert vis == "unknown"
        assert text is None

    def test_empty_price_no_config_returns_unknown(self):
        item = {"price": ""}
        vis, text = detect_pricing_visibility(item, _mock_element(), None)
        assert vis == "unknown"
        assert text is None

    def test_missing_price_key_no_config_returns_unknown(self):
        item = {"name": "Widget"}
        vis, text = detect_pricing_visibility(item, _mock_element(), None)
        assert vis == "unknown"
        assert text is None


class TestDetectPricingVisibilityCallForPrice:
    """Text pattern containing 'call' -> call_for_price."""

    def test_call_for_price_pattern_matches(self):
        config = MapDetectionConfig(text_patterns=["call for price"])
        item = {"price": None}
        el = _mock_element(text="Call for Price")
        vis, text = detect_pricing_visibility(item, el, config)
        assert vis == "call_for_price"
        assert text is None

    def test_call_pattern_case_insensitive(self):
        config = MapDetectionConfig(text_patterns=["CALL FOR PRICE"])
        item = {"price": None}
        el = _mock_element(text="call for price on this item")
        vis, text = detect_pricing_visibility(item, el, config)
        assert vis == "call_for_price"
        assert text is None

    def test_call_pattern_substring_match(self):
        config = MapDetectionConfig(text_patterns=["please call us for pricing"])
        item = {"price": None}
        el = _mock_element(text="Please call us for pricing details")
        vis, text = detect_pricing_visibility(item, el, config)
        assert vis == "call_for_price"
        assert text is None


class TestDetectPricingVisibilityMap:
    """Pattern matches + display text captured -> map."""

    def test_text_pattern_with_display_text(self):
        config = MapDetectionConfig(
            text_patterns=["add to cart to see price"]
        )
        item = {"price": None}
        el = _mock_element(text="Add to Cart to See Price")
        vis, text = detect_pricing_visibility(item, el, config)
        assert vis == "map"
        assert text == "Add to Cart to See Price"

    def test_css_selector_with_display_text(self):
        child = _mock_element(text="See Price In Cart")
        config = MapDetectionConfig(css_selectors=[".map-price-message"])
        item = {"price": None}
        el = _mock_element(
            text="Product XYZ",
            css_results={".map-price-message": [child]},
        )
        vis, text = detect_pricing_visibility(item, el, config)
        assert vis == "map"
        assert text == "See Price In Cart"

    def test_text_pattern_preserves_original_case(self):
        config = MapDetectionConfig(text_patterns=["price too low to advertise"])
        item = {"price": None}
        el = _mock_element(text="Price Too Low To Advertise")
        vis, text = detect_pricing_visibility(item, el, config)
        assert vis == "map"
        assert text == "Price Too Low To Advertise"

    def test_text_pattern_uses_descendant_text_on_real_adaptor(self):
        html = '<div class="product"><span class="map-copy">Add to Cart to See Price</span></div>'
        el = Adaptor(html).css(".product")[0]
        config = MapDetectionConfig(text_patterns=["add to cart to see price"])
        item = {"price": None}
        vis, text = detect_pricing_visibility(item, el, config)
        assert vis == "map"
        assert text == "Add to Cart to See Price"

    def test_embedded_digits_in_price_are_not_treated_as_numeric(self):
        config = MapDetectionConfig(text_patterns=["map 0"])
        item = {"price": "MAP 0"}
        el = _mock_element(text="MAP 0")
        vis, text = detect_pricing_visibility(item, el, config)
        assert vis == "map"
        assert text == "MAP 0"


class TestDetectPricingVisibilityCartOnly:
    """Pattern matches but no display text -> cart_only."""

    def test_css_selector_no_text(self):
        child = _mock_element(text="")
        config = MapDetectionConfig(css_selectors=["[data-map-restricted]"])
        item = {"price": None}
        el = _mock_element(
            text="",
            css_results={"[data-map-restricted]": [child]},
        )
        vis, text = detect_pricing_visibility(item, el, config)
        assert vis == "cart_only"
        assert text is None

    def test_non_numeric_price_value_pattern_match(self):
        config = MapDetectionConfig(price_value_patterns=["see in cart"])
        item = {"price": "see in cart"}
        el = _mock_element(text="")
        vis, text = detect_pricing_visibility(item, el, config)
        assert vis == "cart_only"
        assert text is None

    def test_empty_string_price_value_pattern(self):
        config = MapDetectionConfig(price_value_patterns=[""])
        item = {"price": ""}
        el = _mock_element(text="")
        vis, text = detect_pricing_visibility(item, el, config)
        assert vis == "cart_only"
        assert text is None

    def test_zero_dollar_price_value_pattern_is_explicit(self):
        config = MapDetectionConfig(price_value_patterns=["$0.00"])
        item = {"price": "$0.00"}
        el = _mock_element(text="")
        vis, text = detect_pricing_visibility(item, el, config)
        assert vis == "explicit"
        assert text is None


class TestDetectPricingVisibilityMissing:
    """map_detection exists but nothing matches -> missing."""

    def test_config_present_no_matches(self):
        config = MapDetectionConfig(
            text_patterns=["add to cart to see price"],
            css_selectors=[".map-message"],
            price_value_patterns=["$0.00"],
        )
        item = {"price": None}
        el = _mock_element(text="Some unrelated product text")
        vis, text = detect_pricing_visibility(item, el, config)
        assert vis == "missing"
        assert text is None

    def test_empty_config_no_matches(self):
        config = MapDetectionConfig()
        item = {"price": None}
        el = _mock_element(text="Product details")
        vis, text = detect_pricing_visibility(item, el, config)
        assert vis == "missing"
        assert text is None


class TestDetectStockStatus:
    """Stock status detection from text patterns and CSS selectors."""

    def test_no_config_returns_unknown(self):
        assert detect_stock_status({}, _mock_element(), None) == "unknown"

    def test_in_stock_text_pattern(self):
        config = StockDetectionConfig(
            in_stock=StockPatternConfig(text_patterns=["in stock"]),
        )
        el = _mock_element(text="In Stock - Ships Free")
        assert detect_stock_status({}, el, config) == "in_stock"

    def test_out_of_stock_text_pattern(self):
        config = StockDetectionConfig(
            out_of_stock=StockPatternConfig(text_patterns=["out of stock", "sold out"]),
        )
        el = _mock_element(text="Currently Sold Out")
        assert detect_stock_status({}, el, config) == "out_of_stock"

    def test_limited_stock_text_pattern(self):
        config = StockDetectionConfig(
            limited_stock=StockPatternConfig(text_patterns=["only", "low stock"]),
        )
        el = _mock_element(text="Only 3 Left!")
        assert detect_stock_status({}, el, config) == "limited_stock"

    def test_backorder_text_pattern(self):
        config = StockDetectionConfig(
            backorder=StockPatternConfig(text_patterns=["backorder"]),
        )
        el = _mock_element(text="Available on Backorder")
        assert detect_stock_status({}, el, config) == "backorder"

    def test_preorder_text_pattern(self):
        config = StockDetectionConfig(
            preorder=StockPatternConfig(text_patterns=["pre-order", "preorder"]),
        )
        el = _mock_element(text="Pre-Order Now")
        assert detect_stock_status({}, el, config) == "preorder"

    def test_css_selector_match(self):
        child = _mock_element(text="")
        config = StockDetectionConfig(
            in_stock=StockPatternConfig(css_selectors=[".in-stock-badge"]),
        )
        el = _mock_element(text="", css_results={".in-stock-badge": [child]})
        assert detect_stock_status({}, el, config) == "in_stock"

    def test_stock_signal_extracted_signal_drives_classification_when_dom_text_is_blank(self):
        config = StockDetectionConfig(
            backorder=StockPatternConfig(text_patterns=["backorder"]),
        )
        item = {"stock_signal": "Available on Backorder"}
        el = _mock_element(text="")
        assert detect_stock_status(item, el, config) == "backorder"

    def test_list_valued_stock_signal_drives_classification_when_dom_text_is_blank(self):
        config = StockDetectionConfig(
            limited_stock=StockPatternConfig(text_patterns=["low stock"]),
        )
        item = {"stock_signal": ["Low Stock", "Ships Free"]}
        el = _mock_element(text="")
        assert detect_stock_status(item, el, config) == "limited_stock"

    def test_priority_out_of_stock_over_in_stock(self):
        """When both match, out_of_stock wins (higher priority)."""
        config = StockDetectionConfig(
            in_stock=StockPatternConfig(text_patterns=["stock"]),
            out_of_stock=StockPatternConfig(text_patterns=["out of stock"]),
        )
        el = _mock_element(text="Out of Stock")
        assert detect_stock_status({}, el, config) == "out_of_stock"

    def test_no_match_returns_unknown(self):
        config = StockDetectionConfig(
            in_stock=StockPatternConfig(text_patterns=["in stock"]),
        )
        el = _mock_element(text="Some unrelated text")
        assert detect_stock_status({}, el, config) == "unknown"

    def test_case_insensitive_matching(self):
        config = StockDetectionConfig(
            in_stock=StockPatternConfig(text_patterns=["IN STOCK"]),
        )
        el = _mock_element(text="in stock")
        assert detect_stock_status({}, el, config) == "in_stock"

    def test_css_selector_fallback_still_works_when_stock_signal_text_does_not_match(self):
        child = _mock_element(text="")
        config = StockDetectionConfig(
            in_stock=StockPatternConfig(css_selectors=[".in-stock-badge"]),
        )
        item = {"stock_signal": "Ships Tomorrow"}
        el = _mock_element(text="", css_results={".in-stock-badge": [child]})
        assert detect_stock_status(item, el, config) == "in_stock"

    def test_stock_detection_uses_descendant_text_on_real_adaptor(self):
        html = '<div class="availability"><span class="status">In Stock - Ships Free</span></div>'
        el = Adaptor(html).css(".availability")[0]
        config = StockDetectionConfig(
            in_stock=StockPatternConfig(text_patterns=["in stock"]),
        )
        assert detect_stock_status({}, el, config) == "in_stock"


class TestEnrichItemDetection:
    """enrich_item_detection adds fields to item dicts."""

    def test_adds_all_fields_with_both_configs(self):
        map_cfg = MapDetectionConfig(text_patterns=["add to cart to see price"])
        stock_cfg = StockDetectionConfig(
            in_stock=StockPatternConfig(text_patterns=["in stock"])
        )
        item = {"name": "Widget", "price": None}
        el = _mock_element(text="Add to Cart to See Price - In Stock")
        enrich_item_detection(item, el, map_cfg, stock_cfg)
        assert item["pricing_visibility"] == "map"
        assert item["display_price_text"] == "Add to Cart to See Price"
        assert item["stock_status"] == "in_stock"

    def test_explicit_price_with_stock(self):
        stock_cfg = StockDetectionConfig(
            out_of_stock=StockPatternConfig(text_patterns=["out of stock"]),
        )
        item = {"name": "Widget", "price": "$49.99"}
        el = _mock_element(text="Out of Stock")
        enrich_item_detection(item, el, None, stock_cfg)
        assert item["pricing_visibility"] == "explicit"
        assert item["display_price_text"] is None
        assert item["stock_status"] == "out_of_stock"

    def test_preserves_raw_stock_status_as_stock_signal_before_canonicalizing(self):
        stock_cfg = StockDetectionConfig(
            in_stock=StockPatternConfig(text_patterns=["in stock"]),
        )
        item = {"name": "Widget", "price": None, "stock_status": "In Stock"}
        el = _mock_element(text="In Stock")
        enrich_item_detection(item, el, None, stock_cfg)
        assert item["stock_signal"] == "In Stock"
        assert item["stock_status"] == "in_stock"

    def test_falls_back_to_legacy_stock_status_when_stock_signal_is_blank(self):
        stock_cfg = StockDetectionConfig(
            in_stock=StockPatternConfig(text_patterns=["in stock"]),
        )
        item = {
            "name": "Widget",
            "price": None,
            "stock_signal": "   ",
            "stock_status": "In Stock",
        }
        el = _mock_element(text="In Stock")
        enrich_item_detection(item, el, None, stock_cfg)
        assert item["stock_signal"] == "In Stock"
        assert item["stock_status"] == "in_stock"

    def test_legacy_stock_status_fallback_drives_canonical_status_when_dom_text_is_blank(self):
        stock_cfg = StockDetectionConfig(
            in_stock=StockPatternConfig(text_patterns=["in stock"]),
        )
        item = {"name": "Widget", "price": None, "stock_status": "In Stock"}
        el = _mock_element(text="")
        enrich_item_detection(item, el, None, stock_cfg)
        assert item["stock_signal"] == "In Stock"
        assert item["stock_status"] == "in_stock"

    def test_preserves_list_valued_legacy_stock_status_as_stock_signal(self):
        stock_cfg = StockDetectionConfig(
            in_stock=StockPatternConfig(text_patterns=["in stock"]),
        )
        item = {
            "name": "Widget",
            "price": None,
            "stock_status": ["In Stock", "Ships Free"],
        }
        el = _mock_element(text="In Stock")
        enrich_item_detection(item, el, None, stock_cfg)
        assert item["stock_signal"] == ["In Stock", "Ships Free"]
        assert item["stock_status"] == "in_stock"

    def test_list_valued_legacy_stock_status_fallback_drives_canonical_status_when_dom_text_is_blank(
        self,
    ):
        stock_cfg = StockDetectionConfig(
            in_stock=StockPatternConfig(text_patterns=["in stock"]),
        )
        item = {
            "name": "Widget",
            "price": None,
            "stock_status": ["In Stock", "Ships Free"],
        }
        el = _mock_element(text="")
        enrich_item_detection(item, el, None, stock_cfg)
        assert item["stock_signal"] == ["In Stock", "Ships Free"]
        assert item["stock_status"] == "in_stock"

    @pytest.mark.parametrize(
        "raw_stock",
        [None, "", "   ", [], ["", "   "], ["   "], ()],
    )
    def test_does_not_invent_stock_signal_when_legacy_raw_value_is_empty(self, raw_stock):
        stock_cfg = StockDetectionConfig(
            in_stock=StockPatternConfig(text_patterns=["in stock"]),
        )
        item = {"name": "Widget", "price": None, "stock_status": raw_stock}
        el = _mock_element(text="In Stock")
        enrich_item_detection(item, el, None, stock_cfg)
        assert "stock_signal" not in item
        assert item["stock_status"] == "in_stock"

    def test_no_configs_adds_defaults(self):
        item = {"name": "Widget", "price": "$10.00"}
        el = _mock_element(text="")
        enrich_item_detection(item, el, None, None)
        assert item["pricing_visibility"] == "explicit"
        assert item["display_price_text"] is None
        assert item["stock_status"] == "unknown"

    def test_css_display_text_uses_full_text_not_adaptor_none_sentinel(self):
        html = '<div class="map-message"><span>Add to Cart to See Price</span></div>'
        el = Adaptor(html).css(".map-message")[0]
        map_cfg = MapDetectionConfig(css_selectors=[".map-message"])
        item = {"name": "Widget", "price": None}
        vis, text = detect_pricing_visibility(item, el, map_cfg)
        assert vis == "map"
        assert text == "Add to Cart to See Price"
