"""Narrow compatibility adaptations for the supported Scrapling release."""

from __future__ import annotations

from importlib import import_module
from typing import Any

from scrapling import parser as scrapling_parser


_html_parser: Any = vars(import_module("lxml.html"))["HTMLParser"]


def install_scrapling_compatibility() -> None:
    """Remove Scrapling's deprecated, documented no-op lxml parser argument."""

    namespace = vars(scrapling_parser)
    if getattr(namespace["HTMLParser"], "__module__", "") == __name__:
        return

    def compatible_html_parser(*args: Any, **kwargs: Any) -> Any:
        kwargs.pop("strip_cdata", None)
        return _html_parser(*args, **kwargs)

    compatible_html_parser.__module__ = __name__
    namespace["HTMLParser"] = compatible_html_parser
