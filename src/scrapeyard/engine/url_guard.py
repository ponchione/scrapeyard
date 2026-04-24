"""URL safety checks to prevent SSRF on scrape targets and webhook destinations."""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from urllib.parse import urlparse, urlunparse

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

# Userinfo (user:pass@) inside http(s) URLs that we scrub before returning stored
# config YAML to clients. The host portion is preserved.
_USERINFO_IN_URL_RE = re.compile(
    r"(?P<scheme>https?://)[^/\s:@\"']+:[^/\s@\"']+@",
    re.IGNORECASE,
)


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


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

    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in allowed_schemes:
        raise UnsafeURLError(f"URL scheme {scheme!r} is not allowed")

    host = parsed.hostname
    if not host:
        raise UnsafeURLError("URL has no hostname")

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
    """Strip ``user:pass@`` from any http(s) URL embedded in *text*.

    Used on stored YAML before it is returned to API clients so proxy
    credentials do not leak through ``GET /jobs/{id}``.
    """

    return _USERINFO_IN_URL_RE.sub(lambda m: m.group("scheme"), text)


def redact_userinfo_in_url(url: str) -> str:
    """Return *url* with any userinfo component removed."""

    parsed = urlparse(url)
    if not parsed.username and not parsed.password:
        return url
    host = parsed.hostname or ""
    netloc = host
    if parsed.port:
        netloc = f"{host}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))
