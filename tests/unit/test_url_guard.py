from __future__ import annotations

import pytest

from scrapeyard.engine.url_guard import (
    UnsafeURLError,
    assert_public_url,
    redact_sensitive_config_text,
    redact_sensitive_mapping,
    redact_userinfo_in_text,
    redact_userinfo_in_url,
    url_host_label,
)


def test_assert_public_url_rejects_non_global_cgnat_address() -> None:
    with pytest.raises(UnsafeURLError, match="non-public"):
        assert_public_url("http://100.64.0.1/resource", resolve_dns=False)


def test_assert_public_url_allows_global_literal_address_without_dns() -> None:
    assert_public_url("http://8.8.8.8/resource", resolve_dns=False)


@pytest.mark.parametrize(
    "url",
    [
        "http://[64:ff9b::a9fe:a9fe]/resource",
        "http://[64:ff9b:1::c0a8:1]/resource",
    ],
)
def test_assert_public_url_rejects_nat64_private_ipv4_literals(url: str) -> None:
    with pytest.raises(UnsafeURLError, match="non-public"):
        assert_public_url(url, resolve_dns=False)


def test_assert_public_url_allows_nat64_global_ipv4_literal_without_dns() -> None:
    assert_public_url("http://[64:ff9b::808:808]/resource", resolve_dns=False)


def test_assert_public_url_rejects_invalid_port() -> None:
    with pytest.raises(UnsafeURLError, match="port"):
        assert_public_url("https://example.com:99999/resource", resolve_dns=False)


def test_assert_public_url_rejects_malformed_ipv6_url() -> None:
    with pytest.raises(UnsafeURLError, match="malformed"):
        assert_public_url("http://[::1", resolve_dns=False)


def test_assert_public_url_rejects_empty_dns_root_hostname() -> None:
    with pytest.raises(UnsafeURLError, match="hostname"):
        assert_public_url("http://.", resolve_dns=False)


def test_assert_public_url_rejects_raw_backslash() -> None:
    with pytest.raises(UnsafeURLError, match="backslashes"):
        assert_public_url("http://127.0.0.1\\@example.com/resource", resolve_dns=False)


def test_assert_public_url_rejects_raw_whitespace() -> None:
    with pytest.raises(UnsafeURLError, match="whitespace"):
        assert_public_url("http://127.0.0.1\t@example.com/resource", resolve_dns=False)


def test_assert_public_url_rejects_raw_control_characters() -> None:
    with pytest.raises(UnsafeURLError, match="control characters"):
        assert_public_url("https://example.com/path\x00secret", resolve_dns=False)


@pytest.mark.parametrize(
    "host",
    ["localhost", "localhost.", "localhost.localdomain", "ip6-localhost", "broadcasthost"],
)
def test_assert_public_url_rejects_known_local_hostnames_without_dns(host: str) -> None:
    with pytest.raises(UnsafeURLError, match="blocked"):
        assert_public_url(f"http://{host}/resource", resolve_dns=False)


def test_assert_public_url_rejects_percent_encoded_hostname() -> None:
    with pytest.raises(UnsafeURLError, match="percent escapes"):
        assert_public_url("http://%31%32%37.0.0.1/resource", resolve_dns=False)


def test_assert_public_url_rejects_legacy_ipv4_loopback_without_dns() -> None:
    with pytest.raises(UnsafeURLError, match="non-public"):
        assert_public_url("http://2130706433/resource", resolve_dns=False)


def test_assert_public_url_rejects_trailing_dot_ipv4_loopback_without_dns() -> None:
    with pytest.raises(UnsafeURLError, match="non-public"):
        assert_public_url("http://127.0.0.1./resource", resolve_dns=False)


def test_assert_public_url_rejects_trailing_dot_legacy_ipv4_without_dns() -> None:
    with pytest.raises(UnsafeURLError, match="non-public"):
        assert_public_url("http://2130706433./resource", resolve_dns=False)


def test_assert_public_url_rejects_legacy_ipv4_private_octal_without_dns() -> None:
    with pytest.raises(UnsafeURLError, match="non-public"):
        assert_public_url("http://0300.0250.0001.0001/resource", resolve_dns=False)


def test_redact_userinfo_in_text_handles_passwordless_userinfo() -> None:
    text = "proxy: http://token@gate.example.com:7777"

    assert redact_userinfo_in_text(text) == "proxy: http://gate.example.com:7777"


def test_redact_userinfo_in_text_handles_non_http_schemes() -> None:
    text = "proxy: socks5://user:pass@gate.example.com:1080 cdp: ws://token@browser.example/devtools"

    assert redact_userinfo_in_text(text) == (
        "proxy: socks5://gate.example.com:1080 cdp: ws://browser.example/devtools"
    )


def test_redact_userinfo_in_url_preserves_ipv6_brackets() -> None:
    assert redact_userinfo_in_url("https://user:pass@[2001:4860:4860::8888]:443/a") == (
        "https://[2001:4860:4860::8888]:443/a"
    )


def test_redact_userinfo_in_url_handles_invalid_port_without_raising() -> None:
    assert redact_userinfo_in_url("https://user:pass@example.com:bad/a") == (
        "https://example.com/a"
    )


def test_redact_userinfo_in_text_handles_malformed_ipv6_without_raising() -> None:
    text = "failed at https://user:pass@[::1/private?api_key=secret"

    assert redact_userinfo_in_text(text) == (
        "failed at https://[::1/private?api_key=<redacted>"
    )


def test_redact_userinfo_in_url_masks_sensitive_query_values() -> None:
    assert redact_userinfo_in_url(
        "https://user:pass@example.com/path?api_key=secret&page=2&session_id=abc"
    ) == "https://example.com/path?api_key=<redacted>&page=2&session_id=<redacted>"


def test_redact_userinfo_in_url_masks_sensitive_fragment_values() -> None:
    assert redact_userinfo_in_url(
        "https://example.com/callback#access_token=secret&state=ok"
    ) == "https://example.com/callback#access_token=<redacted>&state=ok"


def test_redact_userinfo_in_url_masks_fragment_query_values() -> None:
    assert redact_userinfo_in_url(
        "https://example.com/app#/callback?session_id=abc&page=2"
    ) == "https://example.com/app#/callback?session_id=<redacted>&page=2"


def test_redact_userinfo_in_url_masks_signed_url_query_values() -> None:
    assert redact_userinfo_in_url(
        "https://example.com/path?AWSAccessKeyId=akia&sig=abc&key=map-key&page=2"
    ) == (
        "https://example.com/path?"
        "AWSAccessKeyId=<redacted>&sig=<redacted>&key=<redacted>&page=2"
    )


def test_redact_userinfo_in_text_masks_sensitive_query_values() -> None:
    text = "failed at https://example.com/path?access_token=secret&page=2"

    assert redact_userinfo_in_text(text) == (
        "failed at https://example.com/path?access_token=<redacted>&page=2"
    )


def test_url_host_label_strips_userinfo_and_preserves_port() -> None:
    assert url_host_label("https://user:pass@Example.COM:8443/products") == "example.com:8443"


def test_redact_sensitive_mapping_masks_secret_keys_and_url_userinfo() -> None:
    value = {
        "headers": {"Authorization": "Bearer secret", "X-Test": "visible"},
        "nested": {"api_token": "secret"},
        "proxy": "socks5://user:pass@gate.example.com:7777",
    }

    redacted = redact_sensitive_mapping(value)

    assert redacted["headers"] == {"Authorization": "<redacted>", "X-Test": "visible"}
    assert redacted["nested"] == {"api_token": "<redacted>"}
    assert redacted["proxy"] == "socks5://gate.example.com:7777"


def test_redact_sensitive_config_text_masks_yaml_secrets() -> None:
    config_yaml = """
project: demo
name: secret-job
proxy:
  url: socks5://user:pass@gate.example.com:7777
webhook:
  url: https://example.com/hook
  headers:
    Authorization: Bearer webhook-secret
target:
  url: https://example.com?api_key=target-secret&page=2
  browser:
    extra_headers:
      X-API-Key: browser-secret
  selectors:
    title: h1
"""

    redacted = redact_sensitive_config_text(config_yaml)

    assert "webhook-secret" not in redacted
    assert "browser-secret" not in redacted
    assert "target-secret" not in redacted
    assert "user:pass" not in redacted
    assert redacted.count("<redacted>") == 3


def test_redact_sensitive_config_text_rejects_yaml_alias_expansion() -> None:
    config_yaml = "proxy: http://user:pass@example.com\ncopy: &copy [1]\nref: *copy\n"

    redacted = redact_sensitive_config_text(config_yaml)

    assert "user:pass" not in redacted
    assert "*copy" in redacted


def test_redact_sensitive_config_text_masks_secret_keys_when_yaml_is_invalid() -> None:
    config_yaml = """
webhook:
  headers:
    Authorization: Bearer webhook-secret
    X-Test: visible
target:
  url: https://example.com?api_key=target-secret&page=2
copy: &copy [1]
ref: *copy
"""

    redacted = redact_sensitive_config_text(config_yaml)

    assert "webhook-secret" not in redacted
    assert "target-secret" not in redacted
    assert "Authorization: <redacted>" in redacted
    assert "X-Test: visible" in redacted
    assert "*copy" in redacted


def test_redact_sensitive_config_text_masks_invalid_yaml_secret_block_values() -> None:
    config_yaml = """
headers:
  Authorization: |
    Bearer line one
    Bearer line two
copy: &copy [1]
ref: *copy
"""

    redacted = redact_sensitive_config_text(config_yaml)

    assert "Bearer line" not in redacted
    assert redacted.count("<redacted>") >= 2


def test_redact_sensitive_config_text_masks_invalid_yaml_list_item_secret_keys() -> None:
    config_yaml = """
headers:
  - Authorization: Bearer webhook-secret
  - X-Test: visible
copy: &copy [1]
ref: *copy
"""

    redacted = redact_sensitive_config_text(config_yaml)

    assert "webhook-secret" not in redacted
    assert "- Authorization: <redacted>" in redacted
    assert "- X-Test: visible" in redacted


def test_redact_sensitive_config_text_masks_invalid_yaml_explicit_secret_keys() -> None:
    config_yaml = """
headers:
  ? Authorization
  : Bearer webhook-secret
copy: &copy [1]
ref: *copy
"""

    redacted = redact_sensitive_config_text(config_yaml)

    assert "webhook-secret" not in redacted
    assert ": <redacted>" in redacted


def test_redact_sensitive_config_text_masks_invalid_yaml_list_explicit_secret_keys() -> None:
    config_yaml = """
headers:
  - ? Authorization
    : Bearer webhook-secret
copy: &copy [1]
ref: *copy
"""

    redacted = redact_sensitive_config_text(config_yaml)

    assert "webhook-secret" not in redacted
    assert ": <redacted>" in redacted


def test_redact_sensitive_config_text_handles_unhashable_yaml_keys() -> None:
    config_yaml = "? [a]\n: http://user:pass@example.com\n"

    redacted = redact_sensitive_config_text(config_yaml)

    assert "user:pass" not in redacted
    assert "http://example.com" in redacted
