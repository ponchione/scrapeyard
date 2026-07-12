"""YAML configuration loader."""

from __future__ import annotations

import os
import re
from typing import Any

from scrapeyard.common.yaml import load_yaml_mapping
from scrapeyard.config.schema import ScrapeConfig


_SECRET_REFERENCE_RE = re.compile(r"\$\{(SCRAPEYARD_SECRET_[A-Z0-9_]+)\}")


def _resolve_secret_references(value: Any) -> Any:
    """Resolve deployment secret references without changing persisted YAML."""

    if isinstance(value, dict):
        return {
            key: _resolve_secret_references(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_resolve_secret_references(item) for item in value]
    if not isinstance(value, str):
        return value

    def _replace(match: re.Match[str]) -> str:
        variable = match.group(1)
        resolved = os.environ.get(variable)
        if resolved is None:
            raise ValueError(f"Missing deployment secret reference {variable}")
        return resolved

    return _SECRET_REFERENCE_RE.sub(_replace, value)


def load_config(yaml_str: str) -> ScrapeConfig:
    """Parse a YAML string into a validated ScrapeConfig."""
    data = load_yaml_mapping(yaml_str)
    return ScrapeConfig(**_resolve_secret_references(data))
