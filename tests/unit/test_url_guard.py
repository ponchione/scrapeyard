from __future__ import annotations

import pytest

from scrapeyard.engine.url_guard import (
    UnsafeURLError,
    assert_public_url,
    redact_sensitive_config_text,
    redact_sensitive_mapping,
    redact_userinfo_in_text,
)


def test_assert_public_url_rejects_non_global_cgnat_address() -> None:
    with pytest.raises(UnsafeURLError, match="non-public"):
        assert_public_url("http://100.64.0.1/resource", resolve_dns=False)


def test_assert_public_url_allows_global_literal_address_without_dns() -> None:
    assert_public_url("http://8.8.8.8/resource", resolve_dns=False)


def test_redact_userinfo_in_text_handles_passwordless_userinfo() -> None:
    text = "proxy: http://token@gate.example.com:7777"

    assert redact_userinfo_in_text(text) == "proxy: http://gate.example.com:7777"


def test_redact_sensitive_mapping_masks_secret_keys_and_url_userinfo() -> None:
    value = {
        "headers": {"Authorization": "Bearer secret", "X-Test": "visible"},
        "nested": {"api_token": "secret"},
        "proxy": "http://user:pass@gate.example.com:7777",
    }

    redacted = redact_sensitive_mapping(value)

    assert redacted["headers"] == {"Authorization": "<redacted>", "X-Test": "visible"}
    assert redacted["nested"] == {"api_token": "<redacted>"}
    assert redacted["proxy"] == "http://gate.example.com:7777"


def test_redact_sensitive_config_text_masks_yaml_secrets() -> None:
    config_yaml = """
project: demo
name: secret-job
proxy:
  url: http://user:pass@gate.example.com:7777
webhook:
  url: https://example.com/hook
  headers:
    Authorization: Bearer webhook-secret
target:
  url: https://example.com
  browser:
    extra_headers:
      X-API-Key: browser-secret
  selectors:
    title: h1
"""

    redacted = redact_sensitive_config_text(config_yaml)

    assert "webhook-secret" not in redacted
    assert "browser-secret" not in redacted
    assert "user:pass" not in redacted
    assert redacted.count("<redacted>") == 2
