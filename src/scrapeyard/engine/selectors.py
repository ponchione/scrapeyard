"""Selector extraction: applies CSS/XPath selectors and transforms to a Scrapling page."""

from __future__ import annotations

from typing import Any, cast

from scrapeyard.config.schema import SelectorLong, SelectorType, SelectorValue
from scrapeyard.config.transforms import apply_transforms, parse_transform


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


def select_items_strict(page: Any, item_selector: SelectorValue) -> list[Any]:
    return select_elements_strict(page, item_selector, operation="select_items")


def select_elements_strict(
    scope: Any,
    selector: SelectorValue,
    *,
    operation: str,
    field_name: str | None = None,
) -> list[Any]:
    query, sel_type, _ = _unpack_selector(selector)
    return _select_elements(
        scope,
        query,
        sel_type,
        operation=operation,
        field_name=field_name,
    )


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
        result[name] = _extract_selector_value(page, selector, field_name=name)
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
    if element is None:
        return ""
    if isinstance(element, str):
        return element

    direct_text = _text_attr(element)
    get_all_text = getattr(element, "get_all_text", None)
    if callable(get_all_text):
        nested_text = _coerce_text(get_all_text())
        if direct_text.strip() and nested_text:
            return f"{direct_text}\n{nested_text}"
        if nested_text:
            return nested_text

    return direct_text


def _text_attr(element: Any) -> str:
    text = getattr(element, "text", None)
    if callable(text):
        text = text()
    return _coerce_text(text)


def _coerce_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)
