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


def _resolve_secret_references(value: Any, resolved_secrets: dict[str, str]) -> Any:
    """Resolve deployment secret references without changing persisted YAML."""

    if isinstance(value, dict):
        return {
            key: _resolve_secret_references(item, resolved_secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_resolve_secret_references(item, resolved_secrets) for item in value]
    if not isinstance(value, str):
        return value

    def _replace(match: re.Match[str]) -> str:
        variable = match.group(1)
        return resolved_secrets[variable]

    return _SECRET_REFERENCE_RE.sub(_replace, value)


def _literal_project(data: dict[str, Any]) -> str:
    project = data.get("project")
    if not isinstance(project, str) or _SECRET_REFERENCE_RE.search(project):
        raise ValueError(
            "Config project must be a literal string before secret references are resolved"
        )
    return safe_path_part(project, label="project")


def _require_literal_name(data: dict[str, Any]) -> None:
    name = data.get("name")
    if not isinstance(name, str) or _SECRET_REFERENCE_RE.search(name):
        raise ValueError(
            "Config name must be a literal string before secret references are resolved"
        )


def load_config_project(yaml_str: str) -> str:
    """Read the literal project namespace without resolving any secret values."""

    return _literal_project(load_yaml_mapping(yaml_str))


def load_config(yaml_str: str) -> ScrapeConfig:
    """Parse a YAML string into a validated ScrapeConfig."""
    data = load_yaml_mapping(yaml_str)
    references = _secret_references(data)
    resolved_secrets: dict[str, str] = {}
    if references:
        project = _literal_project(data)
        _require_literal_name(data)
        policy = get_settings().parsed_secret_reference_allowlist()
        allowed = policy.get(project, frozenset()) | policy.get("*", frozenset())
        denied = sorted(references - allowed)
        if denied:
            raise ValueError(
                f"Deployment secret reference {denied[0]} is not allowed for project {project!r}"
            )
        for reference in sorted(references):
            resolved = os.environ.get(reference)
            if resolved is None:
                raise ValueError(f"Missing deployment secret reference {reference}")
            resolved_secrets[reference] = resolved
    try:
        config = ScrapeConfig(**_resolve_secret_references(data, resolved_secrets))
    except Exception as exc:
        if not resolved_secrets:
            raise
        # Pydantic includes invalid input values in its error text. Never let a
        # resolved deployment secret escape merely because its destination is invalid.
        from scrapeyard.engine.url_guard import redact_deployment_secrets

        detail = redact_deployment_secrets(str(exc), resolved_secrets.values())
        raise ValueError(detail) from exc
    config._resolved_secret_values = tuple(
        value for value in dict.fromkeys(resolved_secrets.values()) if value
    )
    return config
