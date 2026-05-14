"""YAML configuration loader."""

from __future__ import annotations

from scrapeyard.common.yaml import load_yaml_mapping
from scrapeyard.config.schema import ScrapeConfig


def load_config(yaml_str: str) -> ScrapeConfig:
    """Parse a YAML string into a validated ScrapeConfig."""
    data = load_yaml_mapping(yaml_str)
    return ScrapeConfig(**data)
