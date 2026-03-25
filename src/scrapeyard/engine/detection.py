"""MAP detection and stock status classification for scraped listings."""

from __future__ import annotations

import re
from typing import Any

from scrapeyard.config.schema import MapDetectionConfig


def detect_pricing_visibility(
    item_data: dict[str, Any],
    element: Any,
    config: MapDetectionConfig | None,
) -> tuple[str, str | None]:
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
        return ("explicit", None)

    if config is None:
        return ("unknown", None)

    item_text = _get_element_text(element)

    # Step 1: "call" patterns -> call_for_price
    for pattern in config.text_patterns:
        if "call" in pattern.lower() and _text_contains(item_text, pattern):
            return ("call_for_price", None)

    # Step 2-3: all remaining patterns -> map (with text) or cart_only (without)
    display_text: str | None = None
    matched = False

    # Text patterns (skip "call" patterns already handled)
    for pattern in config.text_patterns:
        if "call" in pattern.lower():
            continue
        if _text_contains(item_text, pattern):
            matched = True
            display_text = _extract_display_text(item_text, pattern)
            break

    # CSS selectors
    if not matched:
        for selector in config.css_selectors:
            hits = _css_select(element, selector)
            if hits:
                matched = True
                for hit in hits:
                    hit_text = _get_element_text(hit).strip()
                    if hit_text:
                        display_text = hit_text
                        break
                break

    # Price value patterns (never produce display text)
    if not matched:
        price_raw = item_data.get("price")
        price_str = str(price_raw) if price_raw is not None else ""
        for pattern in config.price_value_patterns:
            if pattern == price_str:
                matched = True
                break

    if matched:
        return ("map", display_text) if display_text else ("cart_only", None)

    # Config exists but nothing matched
    return ("missing", None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_numeric_price(value: Any) -> bool:
    """Return True if *value* represents a positive numeric price."""
    if value is None:
        return False
    s = str(value).strip()
    if not s:
        return False
    cleaned = re.sub(r"[^\d.]", "", s)
    if not cleaned:
        return False
    try:
        return float(cleaned) > 0
    except ValueError:
        return False


def _text_contains(haystack: str, needle: str) -> bool:
    """Case-insensitive substring search."""
    return needle.lower() in haystack.lower()


def _extract_display_text(full_text: str, pattern: str) -> str:
    """Return the substring of *full_text* matching *pattern*, preserving case."""
    idx = full_text.lower().find(pattern.lower())
    if idx == -1:
        return pattern
    return full_text[idx : idx + len(pattern)]


def _get_element_text(element: Any) -> str:
    """Get text content from a Scrapling element."""
    if element is None:
        return ""
    if isinstance(element, str):
        return element
    text = getattr(element, "text", None)
    return text if isinstance(text, str) else ""


def _css_select(element: Any, selector: str) -> list[Any]:
    """Run a CSS selector on an element, returning matched children."""
    css_fn = getattr(element, "css", None)
    if css_fn is None:
        return []
    try:
        return css_fn(selector)
    except Exception:
        return []
