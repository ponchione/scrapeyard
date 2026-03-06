"""Selector extraction: applies CSS/XPath selectors and transforms to a Scrapling page."""

from __future__ import annotations

from typing import Any

from scrapeyard.config.schema import SelectorLong, SelectorType, SelectorValue
from scrapeyard.config.transforms import apply_transforms, parse_transform


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

        if sel_type == SelectorType.xpath:
            elements = page.xpath(query)
        else:
            elements = page.css(query)

        texts = [_element_text(el) for el in elements]

        if transform_str:
            transforms = [parse_transform(t.strip()) for t in transform_str.split("|")]
            texts = [apply_transforms(t, transforms) for t in texts]

        result[name] = texts[0] if len(texts) == 1 else texts

    return result


def _unpack_selector(selector: SelectorValue) -> tuple[str, SelectorType, str | None]:
    """Normalise a selector value into (query, type, transform)."""
    if isinstance(selector, str):
        return selector, SelectorType.css, None
    if isinstance(selector, SelectorLong):
        return selector.query, selector.type, selector.transform
    # dict form (from YAML parsing before Pydantic validation)
    return selector.query, selector.type, selector.transform  # type: ignore[union-attr]


def _element_text(element: Any) -> str:
    """Extract text from a Scrapling element."""
    if isinstance(element, str):
        return element
    return element.text or ""
