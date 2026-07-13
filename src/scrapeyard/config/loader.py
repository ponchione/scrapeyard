"""YAML configuration loader."""

from __future__ import annotations

import os
import re
from typing import Any

from scrapeyard.common.paths import safe_path_part
from scrapeyard.common.settings import get_settings
from scrapeyard.common.yaml import load_yaml_mapping
from scrapeyard.config.schema import ScrapeConfig


_SECRET_REFERENCE_RE = re.compile(r"\$\{(SCRAPEYARD_SECRET_[A-Z0-9_]+)\}")


def _secret_references(value: Any) -> set[str]:
    if isinstance(value, dict):
        references: set[str] = set()
        for item in value.values():
            references.update(_secret_references(item))
        return references
    if isinstance(value, list):
        references = set()
        for item in value:
            references.update(_secret_references(item))
        return references
    if isinstance(value, str):
        return {match.group(1) for match in _SECRET_REFERENCE_RE.finditer(value)}
    return set()


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


def _literal_project(data: dict[str, Any]) -> str:
    project = data.get("project")
    if not isinstance(project, str) or _SECRET_REFERENCE_RE.search(project):
        raise ValueError(
            "Config project must be a literal string before secret references are resolved"
        )
    return safe_path_part(project, label="project")


def load_config_project(yaml_str: str) -> str:
    """Read the literal project namespace without resolving any secret values."""

    return _literal_project(load_yaml_mapping(yaml_str))


def load_config(yaml_str: str) -> ScrapeConfig:
    """Parse a YAML string into a validated ScrapeConfig."""
    data = load_yaml_mapping(yaml_str)
    references = _secret_references(data)
    if references:
        project = _literal_project(data)
        policy = get_settings().parsed_secret_reference_allowlist()
        allowed = policy.get(project, frozenset()) | policy.get("*", frozenset())
        denied = sorted(references - allowed)
        if denied:
            raise ValueError(
                f"Deployment secret reference {denied[0]} is not allowed for project {project!r}"
            )
    return ScrapeConfig(**_resolve_secret_references(data))
