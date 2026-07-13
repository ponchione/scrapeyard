"""YAML loading helpers for untrusted service inputs."""

from __future__ import annotations

import re
from typing import Any

import yaml
from yaml.events import AliasEvent, MappingStartEvent, SequenceStartEvent
from yaml.nodes import MappingNode

_BOOL_TAG = "tag:yaml.org,2002:bool"
_YAML_1_2_BOOL_RE = re.compile(r"^(?:true|false)$", re.IGNORECASE)
MAX_YAML_NESTING = 50


class YamlNestingError(yaml.YAMLError):
    """Raised before user-controlled collection nesting reaches recursion limits."""


class ScrapeyardSafeLoader(yaml.SafeLoader):
    """SafeLoader variant that rejects aliases and excessive collection depth."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        # PyYAML's parser methods remain untyped in types-PyYAML.
        if self.check_event(AliasEvent):  # type: ignore[no-untyped-call]
            raise yaml.YAMLError("YAML aliases are not supported")
        is_collection = self.check_event(  # type: ignore[no-untyped-call]
            MappingStartEvent,
            SequenceStartEvent,
        )
        if not is_collection:
            return super().compose_node(parent, index)

        depth = int(getattr(self, "_scrapeyard_collection_depth", 0)) + 1
        if depth > MAX_YAML_NESTING:
            raise YamlNestingError(
                f"YAML nesting exceeds {MAX_YAML_NESTING} levels"
            )
        self._scrapeyard_collection_depth = depth
        try:
            return super().compose_node(parent, index)
        finally:
            self._scrapeyard_collection_depth = depth - 1


def _construct_mapping_without_duplicates(
    loader: ScrapeyardSafeLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    seen: set[Any] = set()
    for key_node, _value_node in node.value:
        # This is the narrow adapter boundary around PyYAML's untyped constructor.
        key = loader.construct_object(key_node, deep=deep)  # type: ignore[no-untyped-call]
        try:
            if key in seen:
                raise yaml.YAMLError("Duplicate YAML key")
            seen.add(key)
        except TypeError as exc:
            raise yaml.YAMLError("YAML mapping keys must be hashable") from exc
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


ScrapeyardSafeLoader.yaml_implicit_resolvers = {
    key: [
        (tag, regexp)
        for tag, regexp in resolvers
        if tag != _BOOL_TAG
    ]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
# types-PyYAML does not type the resolver registration API.
ScrapeyardSafeLoader.add_implicit_resolver(  # type: ignore[no-untyped-call]
    _BOOL_TAG, _YAML_1_2_BOOL_RE, list("tTfF")
)

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
