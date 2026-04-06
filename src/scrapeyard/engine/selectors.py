"""Selector extraction: applies CSS/XPath selectors and transforms to a Scrapling page."""

from __future__ import annotations

from typing import Any, cast

from scrapeyard.config.schema import SelectorLong, SelectorType, SelectorValue
from scrapeyard.config.transforms import apply_transforms, parse_transform


def select_items(page: Any, item_selector: SelectorValue) -> list[Any]:
    """Return raw DOM elements matched by *item_selector*.

    Used by the detection pipeline to access elements alongside extracted data.
    """
    query, sel_type, _ = _unpack_selector(item_selector)
    return _select_elements(page, query, sel_type)


def count_selector_matches(scope: Any, selector: SelectorValue) -> int:
    """Return the number of raw matches for *selector* within *scope*."""
    query, sel_type, _ = _unpack_selector(selector)
    return len(_select_elements(scope, query, sel_type))


def extract_selectors(page: Any, selectors: dict[str, SelectorValue]) -> dict[str, Any]:
    """Apply named selectors to a Scrapling page response.

    Parameters
    ----------
    page:
        A Scrapling response/Adaptor object supporting ``.css()`` and ``.xpath()``.
    selectors:
        Mapping of field names to selector definitions (short-form string or
        :class:`SelectorLong`).

    Returns
    -------
    dict[str, Any]
        Mapping of field names to extracted values. Each value is a string
        (first match) or a list of strings (multiple matches).
    """
    result: dict[str, Any] = {}
    for name, selector in selectors.items():
        query, sel_type, transform_str = _unpack_selector(selector)
        elements = _select_elements(page, query, sel_type)

        texts = [_element_text(el) for el in elements]

        if transform_str:
            transforms = [parse_transform(t.strip()) for t in transform_str.split("|")]
            texts = [apply_transforms(t, transforms) for t in texts]

        if len(texts) == 0:
            result[name] = None
        elif len(texts) == 1:
            result[name] = texts[0]
        else:
            result[name] = texts

    return result


def _unpack_selector(selector: SelectorValue) -> tuple[str, SelectorType, str | None]:
    """Normalise a selector value into (query, type, transform)."""
    if isinstance(selector, str):
        return selector, SelectorType.css, None
    if isinstance(selector, SelectorLong):
        return selector.query, selector.type, selector.transform
    # dict form (from YAML parsing before Pydantic validation)
    return selector.query, selector.type, selector.transform  # type: ignore[union-attr]


def _select_elements(scope: Any, query: str, selector_type: SelectorType) -> list[Any]:
    """Select elements from a page or item scope using CSS or XPath."""
    select_fn = getattr(scope, "xpath" if selector_type == SelectorType.xpath else "css", None)
    if select_fn is None:
        return []
    try:
        return cast(list[Any], select_fn(query))
    except Exception:
        return []


def _element_text(element: Any) -> str:
    """Extract text from a Scrapling element."""
    if isinstance(element, str):
        return element
    return element.text or ""
