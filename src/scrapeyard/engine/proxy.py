"""Proxy resolution and URL redaction utilities."""

from __future__ import annotations

from urllib.parse import urlparse

from scrapeyard.config.schema import ProxyConfig, TargetConfig


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
        return None if target.proxy.url == "direct" else target.proxy.url

    if job_proxy is not None:
        return None if job_proxy.url == "direct" else job_proxy.url

    if service_proxy_url:
        return None if service_proxy_url == "direct" else service_proxy_url

    return None


def redact_proxy_url(url: str) -> str:
    """Return 'host:port' from a proxy URL, stripping scheme and credentials."""
    parsed = urlparse(url)
    if parsed.port:
        return f"{parsed.hostname}:{parsed.port}"
    return parsed.hostname or url
