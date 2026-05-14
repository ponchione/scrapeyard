"""YAML loading helpers for untrusted service inputs."""

from __future__ import annotations

from typing import Any

import yaml
from yaml.events import AliasEvent
from yaml.nodes import MappingNode


class ScrapeyardSafeLoader(yaml.SafeLoader):
    """SafeLoader variant that rejects YAML aliases."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(AliasEvent):
            raise yaml.YAMLError("YAML aliases are not supported")
        return super().compose_node(parent, index)


def _construct_mapping_without_duplicates(
    loader: ScrapeyardSafeLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    seen: set[Any] = set()
    for key_node, _value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.YAMLError(f"Duplicate YAML key: {key!r}")
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


ScrapeyardSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping_without_duplicates,
)


def load_yaml_mapping(text: str) -> dict[str, Any]:
    """Load a YAML document as a mapping with service safety checks."""
    data = yaml.load(text, Loader=ScrapeyardSafeLoader)
    if not isinstance(data, dict):
        raise ValueError("YAML root must be a mapping")
    return data
