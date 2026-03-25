"""Unit tests for MAP detection and stock status classification."""

from __future__ import annotations

from unittest.mock import MagicMock

from scrapeyard.config.schema import MapDetectionConfig
from scrapeyard.engine.detection import detect_pricing_visibility


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

    def test_price_value_pattern_match(self):
        config = MapDetectionConfig(price_value_patterns=["$0.00"])
        item = {"price": "$0.00"}
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
