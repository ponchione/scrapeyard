"""Tests for pagination URL resolution."""

from __future__ import annotations

from scrapeyard.engine import pagination
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


def test_pagination_url_key_ignores_fragments_and_normalizes_case() -> None:
    assert pagination.pagination_url_key(
        "HTTPS://Example.COM/products?page=1#reviews"
    ) == pagination.pagination_url_key("https://example.com/products?page=1")


def test_pagination_url_key_normalizes_default_ports() -> None:
    assert pagination.pagination_url_key("https://example.com:443/a") == pagination.pagination_url_key(
        "https://example.com/a"
    )
    assert pagination.pagination_url_key("http://example.com:80/a") == pagination.pagination_url_key(
        "http://example.com/a"
    )


def test_pagination_url_key_preserves_query_strings() -> None:
    assert pagination.pagination_url_key("https://example.com/products?page=1") != pagination.pagination_url_key(
        "https://example.com/products?page=2"
    )
