"""Unit tests for proxy resolution and URL redaction."""

from __future__ import annotations

from typing import Any

from scrapeyard.config.schema import ProxyConfig, TargetConfig
from scrapeyard.engine.proxy import redact_proxy_url, resolve_proxy


# --- Helpers ---

def _target(**overrides: Any) -> TargetConfig:
    base: dict[str, Any] = {"url": "https://example.com", "selectors": {"title": "h1"}}
    base.update(overrides)
    return TargetConfig.model_validate(base)


def _proxy(url: str) -> ProxyConfig:
    return ProxyConfig(url=url)


# --- resolve_proxy ---


class TestResolveProxy:
    """Precedence: target > job > service. 'direct' means no proxy."""

    def test_no_proxy_at_any_level(self):
        assert resolve_proxy(_target(), None, "") is None

    def test_service_default_only(self):
        assert resolve_proxy(_target(), None, "http://svc:8080") == "http://svc:8080"

    def test_job_overrides_service(self):
        assert resolve_proxy(
            _target(), _proxy("http://job:9090"), "http://svc:8080"
        ) == "http://job:9090"

    def test_target_overrides_job(self):
        target = _target(proxy={"url": "http://target:7070"})
        assert resolve_proxy(
            target, _proxy("http://job:9090"), "http://svc:8080"
        ) == "http://target:7070"

    def test_target_overrides_service(self):
        target = _target(proxy={"url": "http://target:7070"})
        assert resolve_proxy(target, None, "http://svc:8080") == "http://target:7070"

    def test_direct_at_target_bypasses_all(self):
        target = _target(proxy={"url": "direct"})
        assert resolve_proxy(
            target, _proxy("http://job:9090"), "http://svc:8080"
        ) is None

    def test_direct_at_job_bypasses_service(self):
        assert resolve_proxy(
            _target(), _proxy("direct"), "http://svc:8080"
        ) is None

    def test_direct_at_service_level(self):
        assert resolve_proxy(_target(), None, "direct") is None

    def test_job_only_no_service(self):
        assert resolve_proxy(
            _target(), _proxy("http://job:9090"), ""
        ) == "http://job:9090"


# --- redact_proxy_url ---


class TestRedactProxyUrl:
    """Strip credentials and scheme, return host:port."""

    def test_full_url_with_credentials(self):
        assert redact_proxy_url("http://user:pass@gate.example.com:7777") == "gate.example.com:7777"

    def test_url_without_credentials(self):
        assert redact_proxy_url("http://gate.example.com:7777") == "gate.example.com:7777"

    def test_url_without_port(self):
        assert redact_proxy_url("http://gate.example.com") == "gate.example.com"

    def test_socks5_url(self):
        assert redact_proxy_url("socks5://user:pass@proxy.example.com:1080") == "proxy.example.com:1080"
