"""Selector extraction: applies CSS/XPath selectors and transforms to a Scrapling page."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any, cast

from scrapeyard.config.schema import SelectorLong, SelectorType, SelectorValue
from scrapeyard.config.transforms import (
    apply_transforms,
    checked_selector_value_size,
    parse_transform_pipeline,
)
from scrapeyard.engine.dom_text import element_text_content

OutputByteReserver = Callable[[int], None]


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
        self.query_sha256 = hashlib.sha256(query.encode("utf-8")).hexdigest()
        field_detail = f" for field '{field_name}'" if field_name else ""
        message = (
            f"Selector execution failed during {operation}{field_detail} "
            f"({selector_type.value}, query_sha256={self.query_sha256}): "
            f"{type(original_exception).__name__}"
        )
        super().__init__(message)

    @property
    def debug(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "field_name": self.field_name,
            "query_sha256": self.query_sha256,
            "selector_type": self.selector_type.value,
            "exception_type": type(self.original_exception).__name__,
        }


def select_items_strict(page: object, item_selector: SelectorValue) -> list[object]:
    return select_elements_strict(page, item_selector, operation="select_items")


def select_elements_strict(
    scope: object,
    selector: SelectorValue,
    *,
    operation: str,
    field_name: str | None = None,
) -> list[object]:
    query, sel_type, _ = _unpack_selector(selector)
    return _select_elements(
        scope,
        query,
        sel_type,
        operation=operation,
        field_name=field_name,
    )


def count_selector_matches_strict(
    scope: object,
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


def extract_selectors_strict(
    page: object,
    selectors: dict[str, SelectorValue],
    *,
    reserve_output_bytes: OutputByteReserver | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    _reserve_json_structure(reserve_output_bytes, 2)
    for name, selector in selectors.items():
        _reserve_json_structure(
            reserve_output_bytes,
            _json_string_serialized_size(name) + 1 + int(bool(result)),
        )
        result[name] = _extract_selector_value(
            page,
            selector,
            field_name=name,
            reserve_output_bytes=reserve_output_bytes,
        )
    return result


def _extract_selector_value(
    page: object,
    selector: SelectorValue,
    *,
    field_name: str,
    reserve_output_bytes: OutputByteReserver | None,
) -> Any:
    query, sel_type, transform_str = _unpack_selector(selector)
    elements = _select_elements(
        page,
        query,
        sel_type,
        operation="extract_selectors",
        field_name=field_name,
    )
    transforms = parse_transform_pipeline(transform_str) if transform_str else []
    texts: list[str] = []
    for element in elements:
        text = _element_text(element)
        checked_selector_value_size(text)
        transformed = apply_transforms(text, transforms)
        checked_selector_value_size(transformed)
        _reserve_json_structure(
            reserve_output_bytes,
            _json_string_serialized_size(transformed),
        )
        texts.append(transformed)
    if len(texts) > 1:
        _reserve_json_structure(reserve_output_bytes, len(texts) + 1)
    return _collapse_selector_values(texts)


def _reserve_json_structure(
    reserve_output_bytes: OutputByteReserver | None,
    amount: int,
) -> None:
    if reserve_output_bytes is not None:
        reserve_output_bytes(amount)


def _json_string_serialized_size(value: str) -> int:
    """Return json.dumps' UTF-8 size for one string without building it."""

    size = 2  # surrounding quotes
    for character in value:
        codepoint = ord(character)
        if character in {'"', "\\"} or character in "\b\t\n\f\r":
            size += 2
        elif codepoint < 0x20 or 0x7F <= codepoint <= 0xFFFF:
            size += 6
        elif codepoint > 0xFFFF:
            size += 12
        else:
            size += 1
    return size


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
    return selector.query, selector.type, selector.transform


def _select_elements(
    scope: object,
    query: str,
    selector_type: SelectorType,
    *,
    operation: str,
    field_name: str | None = None,
) -> list[object]:
    """Select elements from a page or item scope using CSS or XPath."""
    select_fn = getattr(scope, "xpath" if selector_type == SelectorType.xpath else "css", None)
    if select_fn is None:
        return []
    try:
        return cast(list[object], select_fn(query))
    except Exception as exc:
        raise SelectorExecutionError(
            operation=operation,
            query=query,
            selector_type=selector_type,
            field_name=field_name,
            original_exception=exc,
        ) from exc


def _element_text(element: object) -> str:
    """Extract text from a Scrapling element."""
    text = element_text_content(element)
    checked_selector_value_size(text)
    return text
