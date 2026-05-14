"""URL safety checks to prevent SSRF on scrape targets and webhook destinations."""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, quote_plus, urlparse, urlunparse

import yaml
from yaml import YAMLError

logger = logging.getLogger(__name__)


class UnsafeURLError(ValueError):
    """Raised when a URL points at a non-public address."""


_DISALLOWED_HOSTS: frozenset[str] = frozenset(
    {
        "metadata.google.internal",
        "metadata.goog",
        "metadata",
        "instance-data",
        "instance-data.ec2.internal",
    }
)

# URLs embedded in free-form text that we scrub before returning stored config
# YAML, logs, or result metadata to clients.
_URL_IN_TEXT_RE = re.compile(r"[a-z][a-z0-9+.-]*://[^\s\"']+", re.IGNORECASE)

_REDACTED_VALUE = "<redacted>"
_SENSITIVE_EXACT_KEYS = frozenset(
    {
        "authorization",
        "proxyauthorization",
        "xapikey",
        "apikey",
        "api_key",
        "cookie",
        "setcookie",
    }
)
_SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "credential",
    "signature",
    "session",
)


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not ip.is_global or ip.is_multicast


def _hostname_is_blocked(host: str) -> bool:
    return host.lower().rstrip(".") in _DISALLOWED_HOSTS


def assert_public_url(
    url: str,
    *,
    allowed_schemes: tuple[str, ...] = ("http", "https"),
    resolve_dns: bool = True,
) -> None:
    """Reject URLs that point at non-public destinations.

    The check has two layers:

    1. Lexical — reject banned scheme, banned hostnames, or literal private IPs.
    2. DNS (best-effort) — when *resolve_dns* is true and the hostname resolves,
       ensure every resolved address is public. Resolution failures are ignored
       because a fetch against a non-resolving host will fail anyway, which is
       not an SSRF vector.
    """

    if "\\" in url:
        raise UnsafeURLError("URL must not contain backslashes")
    if any(char.isspace() for char in url):
        raise UnsafeURLError("URL must not contain whitespace")

    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in allowed_schemes:
        raise UnsafeURLError(f"URL scheme {scheme!r} is not allowed")

    host = parsed.hostname
    if not host:
        raise UnsafeURLError("URL has no hostname")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise UnsafeURLError("URL port is invalid") from exc

    if _hostname_is_blocked(host):
        raise UnsafeURLError(f"Hostname {host!r} is blocked")

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _ip_is_blocked(literal):
            raise UnsafeURLError(f"URL points at non-public IP {literal}")
        return

    if not resolve_dns:
        return

    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        # Host does not resolve right now — the fetch will fail loudly. Not an
        # SSRF vector; do not block config load over transient DNS issues.
        return

    seen: set[str] = set()
    for *_head, sockaddr in infos:
        ip_str = str(sockaddr[0])
        if ip_str in seen:
            continue
        seen.add(ip_str)
        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if _ip_is_blocked(addr):
            raise UnsafeURLError(
                f"Hostname {host!r} resolves to non-public address {ip_str}"
            )


def redact_userinfo_in_text(text: str) -> str:
    """Strip userinfo and sensitive query values from URLs embedded in *text*.

    Used on stored YAML before it is returned to API clients so proxy
    credentials and URL-bearing tokens do not leak through ``GET /jobs/{id}``.
    """

    return _URL_IN_TEXT_RE.sub(lambda match: redact_userinfo_in_url(match.group(0)), text)


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    normalized = lowered.replace("-", "").replace("_", "")
    return normalized in _SENSITIVE_EXACT_KEYS or any(
        part in lowered for part in _SENSITIVE_KEY_PARTS
    )


def redact_sensitive_mapping(value: Any) -> Any:
    """Recursively redact common secret-bearing keys in JSON-like values."""
    if isinstance(value, Mapping):
        return {
            key: _REDACTED_VALUE if _is_sensitive_key(str(key)) else redact_sensitive_mapping(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_mapping(item) for item in value]
    if isinstance(value, str):
        return redact_userinfo_in_text(value)
    return value


def redact_sensitive_config_text(text: str) -> str:
    """Redact userinfo and common secret keys from stored YAML config text."""
    redacted_text = redact_userinfo_in_text(text)
    try:
        data = yaml.safe_load(redacted_text)
    except YAMLError:
        return redacted_text
    if not isinstance(data, dict | list):
        return redacted_text
    return yaml.safe_dump(redact_sensitive_mapping(data), sort_keys=False)


def _redact_query(query: str) -> str:
    pairs = parse_qsl(query, keep_blank_values=True)
    if not pairs or not any(_is_sensitive_key(key) for key, _value in pairs):
        return query
    redacted_pairs = []
    for key, value in pairs:
        redacted_value = _REDACTED_VALUE if _is_sensitive_key(key) else quote_plus(value)
        redacted_pairs.append(f"{quote_plus(key)}={redacted_value}")
    return "&".join(redacted_pairs)


def redact_userinfo_in_url(url: str) -> str:
    """Return *url* with userinfo and sensitive query values removed."""

    parsed = urlparse(url)
    redacted_query = _redact_query(parsed.query)
    if not parsed.username and not parsed.password and redacted_query == parsed.query:
        return url
    netloc = parsed.netloc
    if parsed.username or parsed.password:
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = host
        try:
            port = parsed.port
        except ValueError:
            port = None
        if port:
            netloc = f"{host}:{port}"
    return urlunparse(parsed._replace(netloc=netloc, query=redacted_query))


def url_host_label(url: str) -> str:
    """Return a hostname[:port] label without userinfo for grouping and paths."""
    parsed = urlparse(url)
    host = (parsed.hostname or parsed.netloc or "unknown-host").lower().rstrip(".")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is None:
        return host
    return f"{host}:{port}"
