"""Unit tests for config schema validators, transforms, and loader."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from scrapeyard.config import (
    MapDetectionConfig,  # noqa: F401
    ScrapeConfig,
    StockDetectionConfig,  # noqa: F401
    TargetConfig,
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

    def test_target_with_item_selector_parses(self):
        config = ScrapeConfig(**_tier1_config(target=_target_dict(item_selector=".product-card")))
        assert config.target is not None
        assert config.target.item_selector == ".product-card"

    def test_target_with_browser_and_adaptive_domain_parses(self):
        config = ScrapeConfig(
            **_tier1_config(
                target=_target_dict(
                    adaptive_domain="example.com",
                    browser={
                        "timeout_ms": 90000,
                        "disable_resources": False,
                        "network_idle": True,
                        "click_selector": "button.accept-age-gate",
                        "click_timeout_ms": 3000,
                        "click_wait_ms": 750,
                        "wait_for_selector": ".product-card a",
                        "wait_ms": 1500,
                    },
                )
            )
        )
        assert config.target is not None
        assert config.target.adaptive_domain == "example.com"
        assert config.target.browser is not None
        assert config.target.browser.timeout_ms == 90000
        assert config.target.browser.disable_resources is False
        assert config.target.browser.network_idle is True
        assert config.target.browser.click_selector == "button.accept-age-gate"
        assert config.target.browser.click_timeout_ms == 3000
        assert config.target.browser.click_wait_ms == 750
        assert config.target.browser.wait_for_selector == ".product-card a"
        assert config.target.browser.wait_ms == 1500


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

    def test_join_raises_not_supported(self):
        with pytest.raises(ValueError, match="list-level operation"):
            parse_transform("join:,")

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

    def test_root_template_yaml_loads(self):
        template_path = Path(__file__).resolve().parents[2] / "template.yaml"
        config = load_config(template_path.read_text())
        assert config.project == "example-project"
        assert config.target is not None
        assert config.target.browser is not None
        assert config.target.adaptive_domain is None

    def test_root_template_yaml_matches_current_output_shape(self):
        template_path = Path(__file__).resolve().parents[2] / "template.yaml"
        raw = yaml.safe_load(template_path.read_text())

        assert raw["output"] == {"group_by": "target"}

    def test_eyebox_smoke_yaml_loads_and_matches_current_shape(self):
        config_path = (
            Path(__file__).resolve().parents[2]
            / "docs/test-configs/brownells-optics-smoke.yaml"
        )
        raw = yaml.safe_load(config_path.read_text())
        config = load_config(config_path.read_text())

        assert raw["project"] == "eyebox"
        assert raw["name"] == "brownells-optics"
        assert raw["adaptive"] is False
        assert raw["webhook"]["on"] == ["complete", "partial"]
        assert raw["output"] == {"group_by": "merge"}
        assert config.webhook is not None
        assert [status.value for status in config.webhook.on] == ["complete", "partial"]
        assert config.execution.fail_strategy.value == "partial"
        assert config.execution.delay_between == 3
        assert config.execution.domain_rate_limit == 2
        assert config.target is not None
        assert config.target.fetcher.value == "dynamic"
        assert "stock_signal" in raw["target"]["selectors"]
        assert "stock_status" not in raw["target"]["selectors"]
        assert raw["target"]["map_detection"]["text_patterns"]
        assert raw["target"]["stock_detection"]["in_stock"]["text_patterns"]
        assert config.target.pagination is not None
        assert config.target.pagination.max_pages == 3

    def test_eyebox_validation_yaml_keeps_detection_contract_in_parity(self):
        config_path = (
            Path(__file__).resolve().parents[2]
            / "docs/test-configs/brownells-optics-validation.yaml"
        )
        raw = yaml.safe_load(config_path.read_text())
        config = load_config(config_path.read_text())

        assert raw["project"] == "eyebox"
        assert raw["name"] == "brownells-optics-validation"
        assert raw["adaptive"] is True
        assert "stock_signal" in raw["target"]["selectors"]
        assert "stock_status" not in raw["target"]["selectors"]
        assert raw["target"]["map_detection"]["text_patterns"]
        assert raw["target"]["stock_detection"]["in_stock"]["text_patterns"]
        assert config.target is not None
        assert config.target.map_detection is not None
        assert config.target.stock_detection is not None


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


class TestProxyConfig:
    """ProxyConfig schema parsing and precedence in target/job configs."""

    def test_target_with_proxy_parses(self):
        config = ScrapeConfig(
            **_tier1_config(
                target=_target_dict(proxy={"url": "http://user:pass@gate.example.com:7777"})
            )
        )
        assert config.target is not None
        assert config.target.proxy is not None
        assert config.target.proxy.url == "http://user:pass@gate.example.com:7777"

    def test_target_with_direct_proxy_parses(self):
        config = ScrapeConfig(
            **_tier1_config(
                target=_target_dict(proxy={"url": "direct"})
            )
        )
        assert config.target.proxy.url == "direct"

    def test_target_without_proxy_defaults_to_none(self):
        config = ScrapeConfig(**_tier1_config())
        assert config.target.proxy is None

    def test_job_level_proxy_parses(self):
        config = ScrapeConfig(
            **_tier1_config(proxy={"url": "http://user:pass@gate.example.com:7777"})
        )
        assert config.proxy is not None
        assert config.proxy.url == "http://user:pass@gate.example.com:7777"

    def test_job_without_proxy_defaults_to_none(self):
        config = ScrapeConfig(**_tier1_config())
        assert config.proxy is None

    def test_tier2_targets_with_mixed_proxy(self):
        config = ScrapeConfig(
            **_tier2_config(
                proxy={"url": "http://job-proxy:8080"},
                targets=[
                    _target_dict(proxy={"url": "http://target-proxy:9090"}),
                    _target_dict(url="https://example.org"),
                ],
            )
        )
        assert config.proxy.url == "http://job-proxy:8080"
        assert config.targets[0].proxy.url == "http://target-proxy:9090"
        assert config.targets[1].proxy is None

    def test_proxy_config_requires_url(self):
        with pytest.raises(ValidationError):
            ScrapeConfig(**_tier1_config(proxy={}))


class TestMapDetectionConfig:
    """MapDetectionConfig schema parsing and validation."""

    def test_target_with_map_detection_parses(self):
        config = ScrapeConfig(
            **_tier1_config(
                target=_target_dict(
                    map_detection={
                        "text_patterns": ["add to cart to see price", "call for price"],
                        "css_selectors": [".map-price-message"],
                        "price_value_patterns": ["<hidden-price>"],
                    }
                )
            )
        )
        assert config.target.map_detection is not None
        assert len(config.target.map_detection.text_patterns) == 2
        assert len(config.target.map_detection.css_selectors) == 1
        assert len(config.target.map_detection.price_value_patterns) == 1

    def test_target_without_map_detection_defaults_to_none(self):
        config = ScrapeConfig(**_tier1_config())
        assert config.target.map_detection is None

    def test_map_detection_empty_lists_default(self):
        config = ScrapeConfig(
            **_tier1_config(target=_target_dict(map_detection={}))
        )
        assert config.target.map_detection is not None
        assert config.target.map_detection.text_patterns == []
        assert config.target.map_detection.css_selectors == []
        assert config.target.map_detection.price_value_patterns == []

    def test_map_detection_yaml_round_trip(self):
        yaml_str = """
project: demo
name: job1
target:
  url: https://example.com
  selectors:
    title: h1
  map_detection:
    text_patterns:
      - "add to cart to see price"
      - "call for price"
    css_selectors:
      - ".map-price-message"
    price_value_patterns:
      - "<hidden-price>"
"""
        config = load_config(yaml_str)
        assert config.target.map_detection is not None
        assert "add to cart to see price" in config.target.map_detection.text_patterns


class TestStockDetectionConfig:
    """StockDetectionConfig schema parsing and validation."""

    def test_target_with_stock_detection_parses(self):
        config = ScrapeConfig(
            **_tier1_config(
                target=_target_dict(
                    stock_detection={
                        "in_stock": {"text_patterns": ["in stock", "available"]},
                        "out_of_stock": {"text_patterns": ["out of stock", "sold out"]},
                    }
                )
            )
        )
        assert config.target.stock_detection is not None
        assert config.target.stock_detection.in_stock is not None
        assert len(config.target.stock_detection.in_stock.text_patterns) == 2
        assert config.target.stock_detection.out_of_stock is not None
        assert config.target.stock_detection.limited_stock is None

    def test_target_without_stock_detection_defaults_to_none(self):
        config = ScrapeConfig(**_tier1_config())
        assert config.target.stock_detection is None

    def test_stock_detection_with_css_selectors(self):
        config = ScrapeConfig(
            **_tier1_config(
                target=_target_dict(
                    stock_detection={
                        "in_stock": {
                            "text_patterns": ["in stock"],
                            "css_selectors": [".in-stock-badge"],
                        },
                    }
                )
            )
        )
        sd = config.target.stock_detection
        assert sd.in_stock.css_selectors == [".in-stock-badge"]

    def test_stock_detection_yaml_round_trip(self):
        yaml_str = """
project: demo
name: job1
target:
  url: https://example.com
  selectors:
    title: h1
  stock_detection:
    in_stock:
      text_patterns: ["in stock"]
    out_of_stock:
      text_patterns: ["out of stock"]
"""
        config = load_config(yaml_str)
        assert config.target.stock_detection is not None
        assert config.target.stock_detection.in_stock is not None
