"""Proxy URL validation, resolution, and redaction utilities."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlparse

from scrapeyard.engine.url_guard import assert_public_url

if TYPE_CHECKING:
    from scrapeyard.config.schema import ProxyConfig, TargetConfig

_DIRECT_PROXY = "direct"
_ALLOWED_PROXY_SCHEMES = frozenset({"http", "https", "socks4", "socks4a", "socks5", "socks5h"})


def normalize_proxy_url(value: str) -> str:
    """Return a trimmed proxy URL or raise ValueError for malformed input."""
    proxy_url = value.strip()
    if proxy_url == _DIRECT_PROXY:
        return proxy_url
    if "\\" in proxy_url:
        raise ValueError("Proxy URL must not contain backslashes")
    if any(char.isspace() for char in proxy_url):
        raise ValueError("Proxy URL must not contain whitespace")

    parsed = urlparse(proxy_url)
    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_PROXY_SCHEMES:
        allowed = ", ".join(sorted(_ALLOWED_PROXY_SCHEMES | {_DIRECT_PROXY}))
        raise ValueError(f"Proxy URL scheme must be one of: {allowed}")
    if not parsed.hostname:
        raise ValueError("Proxy URL must include a hostname")
    if "%" in parsed.hostname:
        raise ValueError("Proxy URL hostname must not contain percent escapes")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("Proxy URL port is invalid") from exc
    return proxy_url


def normalize_public_proxy_url(value: str) -> str:
    """Normalize a user-supplied proxy URL and reject non-public destinations."""
    proxy_url = normalize_proxy_url(value)
    if proxy_url != _DIRECT_PROXY:
        assert_public_url(proxy_url, allowed_schemes=tuple(_ALLOWED_PROXY_SCHEMES))
    return proxy_url


def resolve_proxy(
    target: TargetConfig,
    job_proxy: ProxyConfig | None,
    service_proxy_url: str,
) -> str | None:
    """Resolve the effective proxy URL for a target.

    Precedence: target.proxy > job_proxy > service_proxy_url.
    Returns None if no proxy is configured or if the resolved value is "direct".
    """
    if target.proxy is not None:
        return None if target.proxy.url == _DIRECT_PROXY else target.proxy.url

    if job_proxy is not None:
        return None if job_proxy.url == _DIRECT_PROXY else job_proxy.url

    if service_proxy_url:
        service_proxy_url = service_proxy_url.strip()
        return None if service_proxy_url == _DIRECT_PROXY else service_proxy_url

    return None


def redact_proxy_url(url: str) -> str:
    """Return 'host:port' from a proxy URL, stripping scheme and credentials."""
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        return url
    try:
        port = parsed.port
    except ValueError:
        return host
    if port is not None:
        return f"{host}:{port}"
    return host
