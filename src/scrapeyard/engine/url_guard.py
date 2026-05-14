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

from scrapeyard.common.yaml import ScrapeyardSafeLoader

logger = logging.getLogger(__name__)


class UnsafeURLError(ValueError):
    """Raised when a URL points at a non-public address."""


_DISALLOWED_HOSTS: frozenset[str] = frozenset(
    {
        "broadcasthost",
        "ip6-localhost",
        "ip6-loopback",
        "metadata.google.internal",
        "metadata.goog",
        "metadata",
        "instance-data",
        "instance-data.ec2.internal",
        "localhost",
        "localhost.localdomain",
    }
)

# URLs embedded in free-form text that we scrub before returning stored config
# YAML, logs, or result metadata to clients.
_URL_IN_TEXT_RE = re.compile(r"[a-z][a-z0-9+.-]*://[^\s\"']+", re.IGNORECASE)
_URL_AUTHORITY_RE = re.compile(r"^([a-z][a-z0-9+.-]*://)([^/?#]*)(.*)$", re.IGNORECASE)

_REDACTED_VALUE = "<redacted>"
_SENSITIVE_EXACT_KEYS = frozenset(
    {
        "accesskey",
        "authorization",
        "apikey",
        "awsaccesskeyid",
        "cookie",
        "key",
        "proxyauthorization",
        "sig",
        "signature",
        "setcookie",
        "xapikey",
        "xamzsignature",
        "xgoogsignature",
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

_IPV4_EMBEDDING_PREFIXES = (
    ipaddress.IPv6Network("64:ff9b::/96"),
    ipaddress.IPv6Network("64:ff9b:1::/48"),
)


def _embedded_ipv4_addresses(ip: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    addresses: list[ipaddress.IPv4Address] = []
    if ip.ipv4_mapped is not None:
        addresses.append(ip.ipv4_mapped)
    if ip.sixtofour is not None:
        addresses.append(ip.sixtofour)
    if ip.teredo is not None:
        addresses.extend(ip.teredo)
    for prefix in _IPV4_EMBEDDING_PREFIXES:
        if ip in prefix:
            addresses.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return addresses


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if not ip.is_global or ip.is_multicast:
        return True
    if isinstance(ip, ipaddress.IPv6Address):
        return any(
            not embedded.is_global or embedded.is_multicast
            for embedded in _embedded_ipv4_addresses(ip)
        )
    return False


def _hostname_is_blocked(host: str) -> bool:
    return host.lower().rstrip(".") in _DISALLOWED_HOSTS


def _legacy_ipv4_address(host: str) -> ipaddress.IPv4Address | None:
    """Parse IPv4 forms accepted by socket/http stacks but not ipaddress."""
    try:
        packed = socket.inet_aton(host)
    except OSError:
        return None
    return ipaddress.IPv4Address(packed)


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
    if any(ord(char) < 32 or ord(char) == 127 for char in url):
        raise UnsafeURLError("URL must not contain control characters")

    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise UnsafeURLError("URL is malformed") from exc
    scheme = (parsed.scheme or "").lower()
    if scheme not in allowed_schemes:
        raise UnsafeURLError(f"URL scheme {scheme!r} is not allowed")

    host = parsed.hostname
    if not host or not host.strip("."):
        raise UnsafeURLError("URL has no hostname")
    if "%" in host:
        raise UnsafeURLError("URL hostname must not contain percent escapes")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise UnsafeURLError("URL port is invalid") from exc

    if _hostname_is_blocked(host):
        raise UnsafeURLError(f"Hostname {host!r} is blocked")

    ip_host = host.rstrip(".")
    try:
        literal = ipaddress.ip_address(ip_host)
    except ValueError:
        literal = None
    if literal is not None:
        if _ip_is_blocked(literal):
            raise UnsafeURLError(f"URL points at non-public IP {literal}")
        return

    legacy_ipv4 = _legacy_ipv4_address(ip_host)
    if legacy_ipv4 is not None:
        if _ip_is_blocked(legacy_ipv4):
            raise UnsafeURLError(f"URL points at non-public IP {legacy_ipv4}")
        return

    if not resolve_dns:
        return

    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except UnicodeError as exc:
        raise UnsafeURLError("URL hostname is invalid") from exc
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


def _redact_sensitive_yaml_lines(text: str) -> str:
    """Best-effort fallback for invalid YAML that still contains key/value secrets."""
    lines: list[str] = []
    redacting_indent: int | None = None
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        newline = line[len(body):]
        stripped = body.lstrip()
        indent = len(body) - len(stripped)

        if redacting_indent is not None:
            if stripped and indent <= redacting_indent:
                redacting_indent = None
            else:
                lines.append(f"{body[:indent]}{_REDACTED_VALUE}{newline}" if stripped else line)
                continue

        if not stripped or stripped.startswith("#") or ":" not in body:
            lines.append(line)
            continue

        key, _separator, value = body.partition(":")
        key_name = key.strip().strip("'\"")
        if not _is_sensitive_key(key_name):
            lines.append(line)
            continue

        lines.append(f"{key}: {_REDACTED_VALUE}{newline}")
        if not value.strip() or value.strip() in {"|", ">"}:
            redacting_indent = indent
    return "".join(lines)


def redact_sensitive_config_text(text: str) -> str:
    """Redact userinfo and common secret keys from stored YAML config text."""
    redacted_text = redact_userinfo_in_text(text)
    try:
        data = yaml.load(redacted_text, Loader=ScrapeyardSafeLoader)
    except (TypeError, ValueError, YAMLError):
        return _redact_sensitive_yaml_lines(redacted_text)
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


def _redact_fragment(fragment: str) -> str:
    if not fragment:
        return fragment
    head, separator, query = fragment.partition("?")
    if separator:
        return f"{head}{separator}{_redact_query(query)}"
    if "=" in fragment:
        return _redact_query(fragment)
    return fragment


def _strip_userinfo_fallback(url: str) -> str:
    match = _URL_AUTHORITY_RE.match(url)
    if match is None:
        return url
    prefix, authority, rest = match.groups()
    if "@" not in authority:
        return url
    return f"{prefix}{authority.rsplit('@', 1)[1]}{rest}"


def _redact_malformed_url(url: str) -> str:
    """Best-effort redaction for strings that urlparse cannot parse."""
    redacted = _strip_userinfo_fallback(url)
    before_fragment, fragment_separator, fragment = redacted.partition("#")
    head, query_separator, query = before_fragment.partition("?")
    if query_separator:
        before_fragment = f"{head}?{_redact_query(query)}"
    return f"{before_fragment}{fragment_separator}{_redact_fragment(fragment)}"


def redact_userinfo_in_url(url: str) -> str:
    """Return *url* with userinfo and sensitive query values removed."""

    try:
        parsed = urlparse(url)
    except ValueError:
        return _redact_malformed_url(url)
    redacted_query = _redact_query(parsed.query)
    redacted_fragment = _redact_fragment(parsed.fragment)
    if (
        not parsed.username
        and not parsed.password
        and redacted_query == parsed.query
        and redacted_fragment == parsed.fragment
    ):
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
    return urlunparse(parsed._replace(
        netloc=netloc,
        query=redacted_query,
        fragment=redacted_fragment,
    ))


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
