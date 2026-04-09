"""Selector extraction: applies CSS/XPath selectors and transforms to a Scrapling page."""

from __future__ import annotations

import logging
from typing import Any, cast

from scrapeyard.config.schema import SelectorLong, SelectorType, SelectorValue
from scrapeyard.config.transforms import apply_transforms, parse_transform

logger = logging.getLogger(__name__)


class SelectorExecutionError(Exception):
    """Raised when selector execution fails inside the selector engine."""

    def __init__(
        self,
        *,
        operation: str,
        query: str,
        selector_type: SelectorType,
        original_exception: Exception,
        field_name: str | None = None,
    ) -> None:
        self.operation = operation
        self.query = query
        self.selector_type = selector_type
        self.field_name = field_name
        self.original_exception = original_exception
        field_detail = f" for field '{field_name}'" if field_name else ""
        message = (
            f"Selector execution failed during {operation}{field_detail} "
            f"({selector_type.value}: {query}): {type(original_exception).__name__}: {original_exception}"
        )
        super().__init__(message)

    @property
    def debug(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "field_name": self.field_name,
            "query": self.query,
            "selector_type": self.selector_type.value,
            "exception_type": type(self.original_exception).__name__,
            "exception_message": str(self.original_exception),
        }


def select_items(page: Any, item_selector: SelectorValue) -> list[Any]:
    """Return raw DOM elements matched by *item_selector*.

    Used by the detection pipeline to access elements alongside extracted data.
    """
    try:
        return select_items_strict(page, item_selector)
    except SelectorExecutionError as exc:
        logger.debug("Suppressing selector failure while selecting items: %s", exc, exc_info=exc.original_exception)
        return []


def count_selector_matches(scope: Any, selector: SelectorValue) -> int:
    """Return the number of raw matches for *selector* within *scope*."""
    try:
        return count_selector_matches_strict(scope, selector)
    except SelectorExecutionError as exc:
        logger.debug(
            "Suppressing selector failure while counting selector matches: %s",
            exc,
            exc_info=exc.original_exception,
        )
        return 0


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
        try:
            elements = _select_elements(
                page,
                query,
                sel_type,
                operation="extract_selectors",
                field_name=name,
            )
        except SelectorExecutionError as exc:
            logger.debug(
                "Suppressing selector failure while extracting field '%s': %s",
                name,
                exc,
                exc_info=exc.original_exception,
            )
            result[name] = None
            continue

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


def select_items_strict(page: Any, item_selector: SelectorValue) -> list[Any]:
    query, sel_type, _ = _unpack_selector(item_selector)
    return _select_elements(page, query, sel_type, operation="select_items")


def count_selector_matches_strict(
    scope: Any,
    selector: SelectorValue,
    *,
    field_name: str | None = None,
) -> int:
    query, sel_type, _ = _unpack_selector(selector)
    return len(
        _select_elements(
            scope,
            query,
            sel_type,
            operation="count_selector_matches",
            field_name=field_name,
        )
    )


def extract_selectors_strict(page: Any, selectors: dict[str, SelectorValue]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, selector in selectors.items():
        query, sel_type, transform_str = _unpack_selector(selector)
        elements = _select_elements(
            page,
            query,
            sel_type,
            operation="extract_selectors",
            field_name=name,
        )

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


def _select_elements(
    scope: Any,
    query: str,
    selector_type: SelectorType,
    *,
    operation: str,
    field_name: str | None = None,
) -> list[Any]:
    """Select elements from a page or item scope using CSS or XPath."""
    select_fn = getattr(scope, "xpath" if selector_type == SelectorType.xpath else "css", None)
    if select_fn is None:
        return []
    try:
        return cast(list[Any], select_fn(query))
    except Exception as exc:
        raise SelectorExecutionError(
            operation=operation,
            query=query,
            selector_type=selector_type,
            field_name=field_name,
            original_exception=exc,
        ) from exc


def _element_text(element: Any) -> str:
    """Extract text from a Scrapling element."""
    if isinstance(element, str):
        return element
    return element.text or ""
