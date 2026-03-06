"""YAML configuration loader."""

from __future__ import annotations

import yaml

from scrapeyard.config.schema import ScrapeConfig


def load_config(yaml_str: str) -> ScrapeConfig:
    """Parse a YAML string into a validated ScrapeConfig."""
    data = yaml.safe_load(yaml_str)
    return ScrapeConfig(**data)
