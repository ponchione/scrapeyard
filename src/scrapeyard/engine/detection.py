"""MAP detection and stock status classification for scraped listings."""

from __future__ import annotations

import hashlib
import logging
import math
import re
from typing import Any, cast

from scrapeyard.common.budgets import RunBudget
from scrapeyard.config.schema import (
    MapDetectionConfig,
    PricingVisibility,
    StockDetectionConfig,
    StockPatternConfig,
    StockStatus,
)

logger = logging.getLogger(__name__)

_CALL_PATTERN_RE = re.compile(r"\bcall\b", re.IGNORECASE)

_NUMERIC_PRICE_RE = re.compile(
    r"^\s*(?:(?:USD|CAD|AUD|EUR|GBP|JPY)\s+|[$€£¥]\s*)?"
    r"(?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d+)?(?:\s*ea)?\s*$",
    re.IGNORECASE,
)

_PRICE_RANGE_RE = re.compile(
    r"^\s*(?:from\s+)?"
    r"(?:(?:USD|CAD|AUD|EUR|GBP|JPY)\s+|[$€£¥]\s*)?"
    r"(?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d+)?"
    r"(?:\s*(?:-|to)\s*"
    r"(?:(?:USD|CAD|AUD|EUR|GBP|JPY)\s+|[$€£¥]\s*)?"
    r"(?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d+)?)?"
    r"(?:\s*ea)?\s*$",
    re.IGNORECASE,
)


def enrich_item_detection(
    item_data: dict[str, Any],
    element: object,
    map_config: MapDetectionConfig | None,
    stock_config: StockDetectionConfig | None,
    *,
    budget: RunBudget | None = None,
) -> None:
    """Add pricing_visibility, display_price_text, and stock_status to *item_data* in-place."""
    vis, display_text = detect_pricing_visibility(
        item_data,
        element,
        map_config,
        budget=budget,
    )
    item_data["pricing_visibility"] = vis
    item_data["display_price_text"] = display_text
    if not _has_usable_stock_signal(item_data.get("stock_signal")):
        raw_stock = item_data.get("stock_status")
        if _has_usable_stock_signal(raw_stock):
            item_data["stock_signal"] = raw_stock
    item_data["stock_status"] = detect_stock_status(
        item_data,
        element,
        stock_config,
        budget=budget,
    )


def detect_pricing_visibility(
    item_data: dict[str, Any],
    element: object,
    config: MapDetectionConfig | None,
    *,
    budget: RunBudget | None = None,
) -> tuple[PricingVisibility, str | None]:
    """Classify a listing's pricing visibility.

    Parameters
    ----------
    item_data:
        Extracted field dict for one listing (must contain ``"price"`` key).
    element:
        Raw DOM element (Scrapling Adaptor) for CSS/text inspection.
    config:
        MAP detection config from the retailer YAML, or ``None``.

    Returns
    -------
    tuple[str, str | None]
        ``(pricing_visibility, display_price_text)``.
        ``display_price_text`` is non-null only when ``pricing_visibility == 'map'``.
    """
    if _is_numeric_price(item_data.get("price")):
        return (PricingVisibility.explicit, None)

    if config is None:
        return (PricingVisibility.unknown, None)

    _check_budget(budget)
    item_text = _get_element_text(element)
    text_visibility, display_text = _match_map_text_patterns(
        item_text,
        config.text_patterns,
        budget=budget,
    )
    if text_visibility is not None:
        return (text_visibility, display_text)

    css_matched, display_text = _match_map_css_selectors(
        element,
        config.css_selectors,
        budget=budget,
    )
    if css_matched:
        return (
            (PricingVisibility.map, display_text)
            if display_text
            else (PricingVisibility.cart_only, None)
        )

    if _match_map_price_value(
        item_data.get("price"),
        config.price_value_patterns,
        budget=budget,
    ):
        return (PricingVisibility.cart_only, None)

    return (PricingVisibility.missing, None)


# ---------------------------------------------------------------------------
# Stock status detection
# ---------------------------------------------------------------------------

# Priority order for stock status detection — most restrictive first.
_STOCK_PRIORITY = [
    StockStatus.out_of_stock,
    StockStatus.backorder,
    StockStatus.preorder,
    StockStatus.limited_stock,
    StockStatus.in_stock,
]


def detect_stock_status(
    item_data: dict[str, Any],
    element: object,
    config: StockDetectionConfig | None,
    *,
    budget: RunBudget | None = None,
) -> StockStatus:
    """Classify a listing's stock status.

    Parameters
    ----------
    item_data:
        Extracted field dict. ``stock_signal`` is checked first as the raw
        availability text source before falling back to DOM text and CSS selectors.
    element:
        Raw DOM element for CSS/text inspection.
    config:
        Stock detection config from the retailer YAML, or ``None``.

    Returns
    -------
    str
        One of the six canonical ``stock_status`` values.
    """
    if config is None:
        return StockStatus.unknown

    extracted_signal_text = _normalize_stock_signal_text(item_data.get("stock_signal"))
    if extracted_signal_text:
        extracted_signal_normalized = extracted_signal_text.lower()
        for status in _STOCK_PRIORITY:
            _check_budget(budget)
            extracted_patterns: StockPatternConfig | None = getattr(config, status.value, None)
            if extracted_patterns is None:
                continue
            if _stock_text_patterns_match(
                extracted_signal_normalized,
                extracted_patterns,
                budget=budget,
            ):
                return status

    _check_budget(budget)
    item_text = _get_element_text(element)
    item_text_normalized = item_text.lower()

    for status in _STOCK_PRIORITY:
        _check_budget(budget)
        patterns: StockPatternConfig | None = getattr(config, status.value, None)
        if patterns is None:
            continue
        if _stock_patterns_match(
            item_text_normalized,
            element,
            patterns,
            budget=budget,
        ):
            return status

    return StockStatus.unknown


def _stock_text_patterns_match(
    normalized_item_text: str,
    patterns: StockPatternConfig,
    *,
    budget: RunBudget | None,
) -> bool:
    """Return True if any text pattern in *patterns* matches *item_text*."""
    for pattern in patterns.text_patterns:
        _check_budget(budget)
        if pattern in normalized_item_text:
            return True
    return False


def _stock_patterns_match(
    normalized_item_text: str,
    element: object,
    patterns: StockPatternConfig,
    *,
    budget: RunBudget | None,
) -> bool:
    """Return True if any text pattern or CSS selector in *patterns* matches."""
    if _stock_text_patterns_match(
        normalized_item_text,
        patterns,
        budget=budget,
    ):
        return True
    for selector in patterns.css_selectors:
        _check_budget(budget)
        if _css_select(element, selector):
            return True
    return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_numeric_price(value: Any) -> bool:
    """Return True if *value* looks like an explicit numeric price or range."""
    if value is None:
        return False
    if isinstance(value, bool):
        return False
    if isinstance(value, int | float):
        return math.isfinite(value)
    s = _normalize_price_text(value)
    if not s:
        return False
    return _NUMERIC_PRICE_RE.fullmatch(s) is not None or _PRICE_RANGE_RE.fullmatch(s) is not None


def _match_map_text_patterns(
    item_text: str,
    patterns: list[str],
    *,
    budget: RunBudget | None,
) -> tuple[PricingVisibility | None, str | None]:
    normalized_item_text = item_text.lower()
    first_map_match: tuple[PricingVisibility, str | None] | None = None
    for pattern in patterns:
        _check_budget(budget)
        match_index = normalized_item_text.find(pattern)
        if match_index < 0:
            continue
        if _CALL_PATTERN_RE.search(pattern):
            return PricingVisibility.call_for_price, None
        if first_map_match is None:
            display_text = item_text[match_index : match_index + len(pattern)]
            first_map_match = (
                PricingVisibility.map if display_text else PricingVisibility.cart_only,
                display_text or None,
            )
    return first_map_match or (None, None)


def _match_map_css_selectors(
    element: object,
    selectors: list[str],
    *,
    budget: RunBudget | None,
) -> tuple[bool, str | None]:
    for selector in selectors:
        _check_budget(budget)
        hits = _css_select(element, selector)
        if not hits:
            continue
        return True, _first_non_empty_element_text(hits)
    return False, None


def _match_map_price_value(
    price_raw: Any,
    patterns: list[str],
    *,
    budget: RunBudget | None,
) -> bool:
    price_str = _normalize_price_text(price_raw)
    for pattern in patterns:
        _check_budget(budget)
        if pattern == price_str:
            return True
    return False


def _first_non_empty_element_text(elements: list[object]) -> str | None:
    for element in elements:
        element_text = _get_element_text(element)
        if element_text:
            return element_text
    return None


def _check_budget(budget: RunBudget | None) -> None:
    if budget is not None:
        budget.check_deadline()


def _get_element_text(element: object) -> str:
    """Get text content from a Scrapling element."""
    if element is None:
        return ""
    if isinstance(element, str):
        return _clean_element_text(element)

    get_all_text = getattr(element, "get_all_text", None)
    if callable(get_all_text):
        text = _clean_element_text(get_all_text())
        if text:
            return text

    return _clean_element_text(getattr(element, "text", None))


def _clean_element_text(value: object) -> str:
    """Clean Scrapling element text: strip whitespace, filter 'None' literals."""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or text == "None":
        return ""
    return text


def _normalize_price_text(value: Any) -> str:
    """Normalize extracted price values into comparable text for detection."""
    return _normalize_matchable_text(value)


def _has_usable_stock_signal(value: Any) -> bool:
    """Return True when *value* contains non-empty raw selector output."""
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list | tuple):
        return any(isinstance(item, str) and item.strip() for item in value)
    return False


def _normalize_stock_signal_text(value: Any) -> str:
    """Normalize raw extracted stock signal values into matchable text."""
    return _normalize_matchable_text(value)


def _normalize_matchable_text(value: Any) -> str:
    if isinstance(value, str):
        return _clean_element_text(value)
    if isinstance(value, list | tuple):
        parts = [_clean_element_text(item) for item in value if isinstance(item, str)]
        return " ".join(part for part in parts if part)
    return ""


def _css_select(element: object, selector: str) -> list[object]:
    """Run a CSS selector on an element, returning matched children."""
    css_fn = getattr(element, "css", None)
    if css_fn is None:
        return []
    try:
        return cast(list[object], css_fn(selector))
    except Exception as exc:
        logger.debug(
            "Suppressing detection CSS selector failure "
            "query_sha256=%s exception_type=%s",
            hashlib.sha256(selector.encode("utf-8")).hexdigest(),
            type(exc).__name__,
        )
        return []
