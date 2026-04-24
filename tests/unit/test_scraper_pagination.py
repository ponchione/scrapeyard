"""Tests for pagination URL resolution."""

from __future__ import annotations

from scrapeyard.engine.pagination import resolve_href


class _Element:
    def __init__(self, href: str | None = None, attributes: dict[str, str] | None = None) -> None:
        self.attrib = {}
        self.attributes = attributes or {}
        if href is not None:
            self.attrib["href"] = href


def test_resolves_absolute_url() -> None:
    elem = _Element(href="https://example.com/page2")
    assert resolve_href(elem, "https://example.com/page1") == "https://example.com/page2"


def test_resolves_relative_path() -> None:
    elem = _Element(href="next")
    assert resolve_href(elem, "https://example.com/path/page1") == "https://example.com/path/next"


def test_uses_attributes_fallback() -> None:
    elem = _Element(attributes={"href": "http://example.org/other"})
    assert resolve_href(elem, "http://example.org/") == "http://example.org/other"


def test_none_when_no_href() -> None:
    elem = _Element()
    assert resolve_href(elem, "http://example.com/") is None


def test_relative_with_leading_slash() -> None:
    elem = _Element(href="/search")
    assert resolve_href(elem, "https://example.com/products/page") == "https://example.com/search"
