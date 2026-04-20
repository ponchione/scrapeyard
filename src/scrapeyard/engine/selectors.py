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
    return _select_items_impl(page, item_selector, suppress_failures=True)


def count_selector_matches(scope: Any, selector: SelectorValue) -> int:
    """Return the number of raw matches for *selector* within *scope*."""
    return _count_selector_matches_impl(scope, selector, suppress_failures=True)


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
    return _extract_selectors_impl(page, selectors, suppress_failures=True)


def select_items_strict(page: Any, item_selector: SelectorValue) -> list[Any]:
    return _select_items_impl(page, item_selector, suppress_failures=False)


def count_selector_matches_strict(
    scope: Any,
    selector: SelectorValue,
    *,
    field_name: str | None = None,
) -> int:
    return _count_selector_matches_impl(
        scope,
        selector,
        suppress_failures=False,
        field_name=field_name,
    )


def extract_selectors_strict(page: Any, selectors: dict[str, SelectorValue]) -> dict[str, Any]:
    return _extract_selectors_impl(page, selectors, suppress_failures=False)


def _extract_selectors_impl(
    page: Any,
    selectors: dict[str, SelectorValue],
    *,
    suppress_failures: bool,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, selector in selectors.items():
        try:
            result[name] = _extract_selector_value(page, selector, field_name=name)
        except SelectorExecutionError as exc:
            if not suppress_failures:
                raise
            _log_suppressed_selector_failure(
                "Suppressing selector failure while extracting field '%s': %s",
                exc,
                name,
            )
            result[name] = None
    return result


def _extract_selector_value(page: Any, selector: SelectorValue, *, field_name: str) -> Any:
    query, sel_type, transform_str = _unpack_selector(selector)
    texts = [
        _element_text(element)
        for element in _select_elements(
            page,
            query,
            sel_type,
            operation="extract_selectors",
            field_name=field_name,
        )
    ]
    transformed_texts = _apply_selector_transforms(texts, transform_str)
    return _collapse_selector_values(transformed_texts)


def _select_items_impl(page: Any, item_selector: SelectorValue, *, suppress_failures: bool) -> list[Any]:
    query, sel_type, _ = _unpack_selector(item_selector)
    try:
        return _select_elements(page, query, sel_type, operation="select_items")
    except SelectorExecutionError as exc:
        if not suppress_failures:
            raise
        _log_suppressed_selector_failure(
            "Suppressing selector failure while selecting items: %s",
            exc,
        )
        return []


def _count_selector_matches_impl(
    scope: Any,
    selector: SelectorValue,
    *,
    suppress_failures: bool,
    field_name: str | None = None,
) -> int:
    query, sel_type, _ = _unpack_selector(selector)
    try:
        return len(
            _select_elements(
                scope,
                query,
                sel_type,
                operation="count_selector_matches",
                field_name=field_name,
            )
        )
    except SelectorExecutionError as exc:
        if not suppress_failures:
            raise
        _log_suppressed_selector_failure(
            "Suppressing selector failure while counting selector matches: %s",
            exc,
        )
        return 0


def _apply_selector_transforms(texts: list[str], transform_str: str | None) -> list[str]:
    if not transform_str:
        return texts
    transforms = [parse_transform(part.strip()) for part in transform_str.split("|")]
    return [apply_transforms(text, transforms) for text in texts]


def _collapse_selector_values(texts: list[str]) -> Any:
    if not texts:
        return None
    if len(texts) == 1:
        return texts[0]
    return texts


def _log_suppressed_selector_failure(message: str, exc: SelectorExecutionError, *args: Any) -> None:
    logger.debug(message, *args, exc, exc_info=exc.original_exception)


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
