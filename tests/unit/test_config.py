"""Unit tests for config schema validators, transforms, and loader."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scrapeyard.config import (
    ScrapeConfig,
    TargetConfig,
    WebhookConfig,
    WebhookStatus,
    apply_transforms,
    load_config,
    parse_transform,
)


# --- Helpers ---

def _target_dict(**overrides) -> dict:
    base = {"url": "https://example.com", "selectors": {"title": "h1"}}
    base.update(overrides)
    return base


def _tier1_config(**overrides) -> dict:
    base = {"project": "test", "name": "job1", "target": _target_dict()}
    base.update(overrides)
    return base


def _tier2_config(**overrides) -> dict:
    base = {
        "project": "test",
        "name": "job2",
        "targets": [_target_dict(), _target_dict(url="https://example.org")],
    }
    base.update(overrides)
    return base


# --- Schema Validation ---


class TestScrapeConfigValidation:
    """Mutual-exclusivity validation for target/targets."""

    def test_valid_tier1_single_target(self):
        config = ScrapeConfig(**_tier1_config())
        assert config.target is not None
        assert config.targets is None

    def test_valid_tier2_multiple_targets(self):
        config = ScrapeConfig(**_tier2_config())
        assert config.targets is not None
        assert len(config.targets) == 2

    def test_both_target_and_targets_raises(self):
        data = _tier1_config(targets=[_target_dict()])
        with pytest.raises(ValidationError, match="not both"):
            ScrapeConfig(**data)

    def test_neither_target_nor_targets_raises(self):
        data = {"project": "test", "name": "job1"}
        with pytest.raises(ValidationError, match="must be provided"):
            ScrapeConfig(**data)


class TestResolvedTargets:
    """resolved_targets() convenience method."""

    def test_tier1_returns_single_element_list(self):
        config = ScrapeConfig(**_tier1_config())
        result = config.resolved_targets()
        assert len(result) == 1
        assert isinstance(result[0], TargetConfig)

    def test_tier2_returns_targets_list(self):
        config = ScrapeConfig(**_tier2_config())
        result = config.resolved_targets()
        assert len(result) == 2
        assert result is config.targets


# --- Transform Parser ---


class TestParseTransform:
    """parse_transform() for all 8 transform types."""

    def test_trim(self):
        fn = parse_transform("trim")
        assert fn("  hello  ") == "hello"

    def test_lowercase(self):
        fn = parse_transform("lowercase")
        assert fn("HELLO") == "hello"

    def test_uppercase(self):
        fn = parse_transform("uppercase")
        assert fn("hello") == "HELLO"

    def test_prepend(self):
        fn = parse_transform("prepend:prefix_")
        assert fn("value") == "prefix_value"

    def test_append(self):
        fn = parse_transform("append:_suffix")
        assert fn("value") == "value_suffix"

    def test_replace(self):
        fn = parse_transform("replace:old:new")
        assert fn("old value old") == "new value new"

    def test_regex(self):
        fn = parse_transform(r"regex:\d+:NUM")
        assert fn("item 42 and 7") == "item NUM and NUM"

    def test_join_identity_on_single_string(self):
        fn = parse_transform("join:,")
        assert fn("hello") == "hello"

    def test_unknown_transform_raises(self):
        with pytest.raises(ValueError, match="Unknown transform"):
            parse_transform("bogus")

    def test_prepend_missing_value_raises(self):
        with pytest.raises(ValueError, match="prepend requires"):
            parse_transform("prepend")


class TestApplyTransforms:
    """apply_transforms() chaining behavior."""

    def test_chains_multiple_transforms(self):
        transforms = [
            parse_transform("trim"),
            parse_transform("lowercase"),
            parse_transform("append:!"),
        ]
        assert apply_transforms("  HELLO  ", transforms) == "hello!"

    def test_empty_list_returns_unchanged(self):
        assert apply_transforms("value", []) == "value"


# --- YAML Loader ---


class TestLoadConfig:
    """load_config() YAML parsing integration."""

    def test_tier1_yaml(self):
        yaml_str = """
project: demo
name: job1
target:
  url: https://example.com
  selectors:
    title: h1
"""
        config = load_config(yaml_str)
        assert config.project == "demo"
        assert config.target is not None
        assert config.target.url == "https://example.com"

    def test_tier2_yaml(self):
        yaml_str = """
project: demo
name: job2
targets:
  - url: https://example.com
    selectors:
      title: h1
  - url: https://example.org
    selectors:
      heading: h2
"""
        config = load_config(yaml_str)
        assert config.targets is not None
        assert len(config.targets) == 2
        assert config.targets[1].url == "https://example.org"

    def test_invalid_both_target_and_targets_raises(self):
        yaml_str = """
project: demo
name: job3
target:
  url: https://example.com
  selectors:
    title: h1
targets:
  - url: https://example.org
    selectors:
      heading: h2
"""
        with pytest.raises(ValidationError, match="not both"):
            load_config(yaml_str)


# --- Webhook Config ---


class TestWebhookConfig:
    """WebhookConfig schema parsing and validation."""

    def test_config_without_webhook_defaults_to_none(self):
        config = ScrapeConfig(**_tier1_config())
        assert config.webhook is None

    def test_valid_webhook_parses(self):
        webhook_data = {
            "url": "https://example.com/hook",
            "on": ["complete", "failed"],
            "headers": {"Authorization": "Bearer token"},
            "timeout": 5,
        }
        config = ScrapeConfig(**_tier1_config(webhook=webhook_data))
        assert config.webhook is not None
        assert str(config.webhook.url) == "https://example.com/hook"
        assert config.webhook.on == [WebhookStatus.complete, WebhookStatus.failed]
        assert config.webhook.headers == {"Authorization": "Bearer token"}
        assert config.webhook.timeout == 5

    def test_webhook_defaults(self):
        webhook_data = {"url": "https://example.com/hook"}
        config = ScrapeConfig(**_tier1_config(webhook=webhook_data))
        assert config.webhook is not None
        assert config.webhook.on == [WebhookStatus.complete, WebhookStatus.partial]
        assert config.webhook.headers == {}
        assert config.webhook.timeout == 10

    def test_invalid_webhook_on_value_raises(self):
        webhook_data = {"url": "https://example.com/hook", "on": ["complet"]}
        with pytest.raises(ValidationError):
            ScrapeConfig(**_tier1_config(webhook=webhook_data))
