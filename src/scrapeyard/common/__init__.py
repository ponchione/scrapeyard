"""Common utilities shared across the scrapeyard service."""

from __future__ import annotations

from typing import Any

__all__ = ["ServiceSettings", "get_settings"]


def __getattr__(name: str) -> Any:
    if name == "ServiceSettings":
        from scrapeyard.common.settings import ServiceSettings

        return ServiceSettings
    if name == "get_settings":
        from scrapeyard.common.settings import get_settings

        return get_settings
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
