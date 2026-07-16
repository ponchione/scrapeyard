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
    # Scrapling represents CSS/XPath text pseudo-elements as lxml
    # ``_ElementUnicodeResult`` objects.  They are string subclasses whose
    # public ``text`` and ``get_all_text()`` accessors both expose the same
    # value; falling through to the compatibility branch would concatenate
    # that value with itself.
    if isinstance(root, str):
        return root

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
    tag = getattr(element, "tag", "")
    local_tag = tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""
    if local_tag in _NON_VISIBLE_TAGS:
        return

    text = getattr(element, "text", None)
    if isinstance(text, str):
        yield text
    for child in cast(Iterable[object], element):
        yield from _visible_text_segments(child)
        tail = getattr(child, "tail", None)
        if isinstance(tail, str):
            yield tail


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
