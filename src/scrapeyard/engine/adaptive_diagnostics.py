"""Adaptive selector diagnostics for scraper extraction."""

from __future__ import annotations

import logging
from typing import Any

from scrapeyard.config.schema import TargetConfig
from scrapeyard.engine.url_guard import redact_userinfo_in_url

logger = logging.getLogger(__name__)


def missing_adaptive_selectors(target: TargetConfig, data: list[dict[str, Any]]) -> list[str]:
    """Return selector names whose extracted values are entirely empty."""
    if not data:
        return list(target.selectors.keys())
    missing: list[str] = []
    for key in target.selectors:
        values = [record.get(key) for record in data]
        if all(value is None or value == "" or value == [] for value in values):
            missing.append(key)
    return missing


def log_adaptive_selector_gap(target: TargetConfig, data: list[dict[str, Any]]) -> None:
    missing = missing_adaptive_selectors(target, data)
    if missing:
        logger.info(
            "Adaptive relocation check: url=%s missing_selectors=%s",
            redact_userinfo_in_url(target.url),
            ",".join(missing),
        )


def has_extracted_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list | tuple | set):
        return any(has_extracted_value(item) for item in value)
    return True
