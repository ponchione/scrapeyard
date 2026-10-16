"""DOM text-content compatibility helpers."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import cast

from lxml import etree  # type: ignore[import-untyped]

_NON_VISIBLE_TAGS = {"script", "style"}


def element_text_content(element: object) -> str:
    """Return an element's complete visible text in document order.

    Scrapling 0.2 exposes direct text and descendant text separately, omits
    descendant tails, and represents missing direct text as the string
    ``"None"``. Its wrapped lxml node retains the complete mixed-content tree,
    so prefer that representation. Text segments are concatenated without an
    invented separator, matching DOM ``textContent`` semantics and preserving
    whitespace supplied by the page.

    The public-accessor fallback keeps lightweight test doubles and compatible
    third-party element objects working.
    """

    if element is None:
        return ""
    if isinstance(element, str):
        return element

    root = getattr(element, "_root", None)
    if etree.iselement(root):
        return "".join(_visible_text_segments(root))

    direct_text = _text_attribute(element)
    get_all_text = getattr(element, "get_all_text", None)
    if not callable(get_all_text):
        return direct_text

    descendant_value = get_all_text()
    descendant_text = descendant_value if isinstance(descendant_value, str) else ""
    if direct_text and descendant_text:
        return f"{direct_text}\n{descendant_text}"
    return descendant_text or direct_text


def _visible_text_segments(element: object) -> Iterator[str]:
    # A stack, not recursion: a page nested deeper than Python's recursion
    # limit would otherwise fail its target. A string on it is a tail.
    pending: list[object] = [element]
    while pending:
        node = pending.pop()
        if isinstance(node, str):
            yield node
            continue
        tag = getattr(node, "tag", "")
        local_tag = tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""
        if local_tag in _NON_VISIBLE_TAGS:
            continue

        text = getattr(node, "text", None)
        if isinstance(text, str):
            yield text
        for child in reversed(list(cast(Iterable[object], node))):
            tail = getattr(child, "tail", None)
            if isinstance(tail, str):
                pending.append(tail)
            pending.append(child)


def _text_attribute(element: object) -> str:
    text = getattr(element, "text", None)
    if callable(text):
        text = text()
    return _coerce_text(text)


def _coerce_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)
