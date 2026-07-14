"""URL safety checks to prevent SSRF on scrape targets and webhook destinations."""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from collections.abc import Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, quote_plus, unquote_plus, urlparse, urlunparse

import yaml
from yaml import YAMLError

from scrapeyard.common.yaml import ScrapeyardSafeLoader

logger = logging.getLogger(__name__)


class UnsafeURLError(ValueError):
    """Raised when a URL points at a non-public address."""


class URLResolutionError(OSError):
    """Raised when DNS cannot currently provide a usable destination."""


@dataclass(frozen=True, slots=True)
class ResolvedPublicURL:
    """A URL whose network connection is pinned to one validated public IP."""

    connect_url: str
    host_header: str
    sni_hostname: str


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
_QUERY_SEPARATOR_RE = re.compile(r"([&;])")

_REDACTED_VALUE = "<redacted>"
_ACTIVE_DEPLOYMENT_SECRETS: ContextVar[tuple[str, ...]] = ContextVar(
    "scrapeyard_active_deployment_secrets",
    default=(),
)
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
_SECRET_CONTAINER_KEYS = frozenset({"headers", "extraheaders"})

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


def _canonical_hostname(host: str) -> str:
    """Return the ASCII hostname form used by Python's resolver."""
    try:
        return host.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as exc:
        raise UnsafeURLError("URL hostname is invalid") from exc


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
    allow_unresolved: bool = True,
) -> None:
    """Reject URLs that point at non-public destinations.

    The check has two layers:

    1. Lexical — reject banned scheme, banned hostnames, or literal private IPs.
    2. DNS — when *resolve_dns* is true and the hostname resolves, ensure every
       resolved address is public. Resolution failures are allowed by default
       because direct fetches against non-resolving hosts fail anyway. Callers
       using proxies or remote browsers should set *allow_unresolved* to false
       because DNS may be resolved outside this process.
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

    host = _canonical_hostname(host)
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

    legacy_ipv4 = _legacy_ipv4_address(host)
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
    except socket.gaierror as exc:
        if not allow_unresolved:
            raise URLResolutionError(f"Hostname {host!r} could not be resolved") from exc
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
            raise UnsafeURLError(f"Hostname {host!r} resolves to non-public address {ip_str}")


def resolve_public_url(url: str) -> ResolvedPublicURL:
    """Resolve once and return connection parameters pinned to a public IP.

    The original hostname is retained for HTTP Host and TLS SNI, while the
    socket destination uses the address validated here. This closes the DNS
    rebinding window for direct HTTP clients.
    """
    assert_public_url(url, resolve_dns=False)
    parsed = urlparse(url)
    original_host = parsed.hostname
    if original_host is None:  # Covered by assert_public_url; keeps typing honest.
        raise UnsafeURLError("URL has no hostname")
    canonical_host = _canonical_hostname(original_host)

    literal: ipaddress.IPv4Address | ipaddress.IPv6Address | None
    try:
        literal = ipaddress.ip_address(canonical_host)
    except ValueError:
        literal = _legacy_ipv4_address(canonical_host)

    if literal is None:
        try:
            infos = socket.getaddrinfo(canonical_host, parsed.port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise URLResolutionError(f"Hostname {canonical_host!r} could not be resolved") from exc
        except UnicodeError as exc:
            raise UnsafeURLError("URL hostname is invalid") from exc
        addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
        seen: set[str] = set()
        for *_head, sockaddr in infos:
            try:
                address = ipaddress.ip_address(str(sockaddr[0]))
            except ValueError:
                continue
            if str(address) in seen:
                continue
            seen.add(str(address))
            if _ip_is_blocked(address):
                raise UnsafeURLError(
                    f"Hostname {canonical_host!r} resolves to non-public address {address}"
                )
            addresses.append(address)
        if not addresses:
            raise URLResolutionError(f"Hostname {canonical_host!r} resolved to no usable address")
        literal = addresses[0]

    ip_authority = f"[{literal}]" if isinstance(literal, ipaddress.IPv6Address) else str(literal)
    if parsed.port is not None:
        ip_authority = f"{ip_authority}:{parsed.port}"
    userinfo = ""
    if "@" in parsed.netloc:
        userinfo = f"{parsed.netloc.rsplit('@', 1)[0]}@"
    connect_url = urlunparse(parsed._replace(netloc=f"{userinfo}{ip_authority}"))

    host_authority = f"[{canonical_host}]" if ":" in canonical_host else canonical_host
    if parsed.port is not None:
        host_authority = f"{host_authority}:{parsed.port}"
    return ResolvedPublicURL(connect_url, host_authority, canonical_host)


def activate_deployment_secret_redaction(
    secret_values: tuple[str, ...],
) -> Token[tuple[str, ...]]:
    """Activate resolved-secret redaction for the current run context."""

    return _ACTIVE_DEPLOYMENT_SECRETS.set(tuple(value for value in secret_values if value))


def reset_deployment_secret_redaction(token: Token[tuple[str, ...]]) -> None:
    """Restore the prior run-scoped secret redaction context."""

    _ACTIVE_DEPLOYMENT_SECRETS.reset(token)


def redact_deployment_secrets(text: str, secret_values: Any = None) -> str:
    """Replace raw and URL-encoded forms of resolved deployment secrets."""

    return redact_deployment_secrets_with_count(text, secret_values)[0]


def redact_deployment_secrets_with_count(
    text: str,
    secret_values: Any = None,
) -> tuple[str, int]:
    """Redact resolved secrets and return the number of replaced substrings."""

    values = (
        _ACTIVE_DEPLOYMENT_SECRETS.get()
        if secret_values is None
        else tuple(value for value in secret_values if isinstance(value, str) and value)
    )
    variants: set[str] = set()
    for value in values:
        variants.update((value, quote(value, safe=""), quote_plus(value, safe="")))
    redaction_count = 0
    for variant in sorted(variants, key=len, reverse=True):
        if variant:
            redaction_count += text.count(variant)
            text = text.replace(variant, _REDACTED_VALUE)
    return text, redaction_count


def redact_deployment_secrets_in_value(
    value: Any,
    *,
    secret_values: Any = None,
) -> Any:
    """Redact only resolved deployment-secret values in JSON-like caller data."""
    return redact_deployment_secrets_in_value_with_count(
        value,
        secret_values=secret_values,
    )[0]


def redact_deployment_secrets_in_value_with_count(
    value: Any,
    *,
    secret_values: Any = None,
) -> tuple[Any, int]:
    """Redact JSON-like caller data and report how many matches were changed."""

    if isinstance(value, Mapping):
        redacted: dict[Any, Any] = {}
        redaction_count = 0
        for key, item in value.items():
            redacted_key: Any = key
            if isinstance(key, str):
                redacted_key, key_count = redact_deployment_secrets_with_count(
                    key,
                    secret_values,
                )
                redaction_count += key_count
                if redacted_key in redacted:
                    base_key = redacted_key
                    suffix = 2
                    while redacted_key in redacted:
                        redacted_key = f"{base_key}#{suffix}"
                        suffix += 1
            redacted_item, item_count = redact_deployment_secrets_in_value_with_count(
                item,
                secret_values=secret_values,
            )
            redacted[redacted_key] = redacted_item
            redaction_count += item_count
        return redacted, redaction_count
    if isinstance(value, list):
        redacted_list_items = [
            redact_deployment_secrets_in_value_with_count(
                item,
                secret_values=secret_values,
            )
            for item in value
        ]
        return (
            [item for item, _count in redacted_list_items],
            sum(count for _item, count in redacted_list_items),
        )
    if isinstance(value, tuple):
        redacted_tuple_items = tuple(
            redact_deployment_secrets_in_value_with_count(
                item,
                secret_values=secret_values,
            )
            for item in value
        )
        return (
            tuple(item for item, _count in redacted_tuple_items),
            sum(count for _item, count in redacted_tuple_items),
        )
    if isinstance(value, str):
        return redact_deployment_secrets_with_count(value, secret_values)
    return value, 0


def redact_userinfo_in_text(text: str) -> str:
    """Strip userinfo and sensitive query values from URLs embedded in *text*.

    Used on stored YAML before it is returned to API clients so proxy
    credentials and URL-bearing tokens do not leak through ``GET /jobs/{id}``.
    """

    redacted = redact_deployment_secrets(text)
    return _URL_IN_TEXT_RE.sub(lambda match: redact_userinfo_in_url(match.group(0)), redacted)


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    normalized = lowered.replace("-", "").replace("_", "")
    return normalized in _SENSITIVE_EXACT_KEYS or any(
        part in lowered for part in _SENSITIVE_KEY_PARTS
    )


def redact_sensitive_mapping(value: Any, *, secret_values: Any = None) -> Any:
    """Recursively redact common secret-bearing keys in JSON-like values."""
    if isinstance(value, Mapping):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            redacted_key: Any = key
            if isinstance(key, str):
                redacted_key = redact_deployment_secrets(key, secret_values)
                if redacted_key in redacted:
                    base_key = redacted_key
                    suffix = 2
                    while redacted_key in redacted:
                        redacted_key = f"{base_key}#{suffix}"
                        suffix += 1
            normalized = str(key).lower().replace("-", "").replace("_", "")
            if _is_sensitive_key(str(key)):
                redacted[redacted_key] = _REDACTED_VALUE
            elif normalized in _SECRET_CONTAINER_KEYS and isinstance(item, Mapping):
                redacted[redacted_key] = {header: _REDACTED_VALUE for header in item}
            else:
                redacted[redacted_key] = redact_sensitive_mapping(
                    item,
                    secret_values=secret_values,
                )
        return redacted
    if isinstance(value, list):
        return [redact_sensitive_mapping(item, secret_values=secret_values) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_mapping(item, secret_values=secret_values) for item in value)
    if isinstance(value, str):
        return redact_userinfo_in_text(redact_deployment_secrets(value, secret_values))
    return value


def _redact_sensitive_yaml_lines(text: str) -> str:
    """Best-effort fallback for invalid YAML that still contains key/value secrets."""
    lines: list[str] = []
    redacting_indent: int | None = None
    pending_sensitive_key_indent: int | None = None
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        newline = line[len(body) :]
        stripped = body.lstrip()
        indent = len(body) - len(stripped)

        if redacting_indent is not None:
            if stripped and indent <= redacting_indent:
                redacting_indent = None
            else:
                lines.append(f"{body[:indent]}{_REDACTED_VALUE}{newline}" if stripped else line)
                continue

        if pending_sensitive_key_indent is not None:
            if stripped.startswith(":"):
                prefix, _separator, value = body.partition(":")
                lines.append(f"{prefix}: {_REDACTED_VALUE}{newline}")
                if not value.strip() or value.strip() in {"|", ">"}:
                    redacting_indent = pending_sensitive_key_indent
                pending_sensitive_key_indent = None
                continue
            if stripped:
                pending_sensitive_key_indent = None

        if not stripped or stripped.startswith("#") or ":" not in body:
            if stripped.startswith(("? ", "- ? ")) and _is_sensitive_key(
                _yaml_fallback_key_name(stripped)
            ):
                pending_sensitive_key_indent = indent
            lines.append(line)
            continue

        key, _separator, value = body.partition(":")
        key_name = _yaml_fallback_key_name(key)
        if not _is_sensitive_key(key_name):
            lines.append(line)
            continue

        lines.append(f"{key}: {_REDACTED_VALUE}{newline}")
        if not value.strip() or value.strip() in {"|", ">"}:
            redacting_indent = indent
    return "".join(lines)


def _yaml_fallback_key_name(raw_key: str) -> str:
    key = raw_key.strip()
    for prefix in ("- ", "? "):
        if key.startswith(prefix):
            key = key[len(prefix) :].lstrip()
    return key.strip("'\"")


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


def _redact_query_part(part: str) -> str:
    key, separator, _value = part.partition("=")
    if not separator:
        return _REDACTED_VALUE if part else part
    key_text = unquote_plus(key)
    return f"{quote_plus(key_text)}={_REDACTED_VALUE}"


def _redact_query(query: str) -> str:
    if not query:
        return query
    parts = _QUERY_SEPARATOR_RE.split(query)
    return "".join(
        part if index % 2 else _redact_query_part(part) for index, part in enumerate(parts)
    )


def _redact_path_params(path: str) -> str:
    if ";" not in path:
        return path
    segments: list[str] = []
    for segment in path.split("/"):
        head, *params = segment.split(";")
        if not params:
            segments.append(segment)
            continue
        redacted_params = [_redact_query_part(param) for param in params]
        segments.append(";".join((head, *redacted_params)))
    return "/".join(segments)


def _redact_fragment(fragment: str) -> str:
    if not fragment:
        return fragment
    return _REDACTED_VALUE


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
    """Return a diagnostic URL with userinfo, query values, and fragment removed."""

    url = redact_deployment_secrets(url)

    try:
        parsed = urlparse(url)
    except ValueError:
        return _redact_malformed_url(url)
    redacted_path = _redact_path_params(parsed.path)
    redacted_params = _redact_query(parsed.params)
    redacted_query = _redact_query(parsed.query)
    redacted_fragment = _redact_fragment(parsed.fragment)
    if (
        not parsed.username
        and not parsed.password
        and redacted_path == parsed.path
        and redacted_params == parsed.params
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
    return urlunparse(
        parsed._replace(
            netloc=netloc,
            path=redacted_path,
            params=redacted_params,
            query=redacted_query,
            fragment=redacted_fragment,
        )
    )


def url_host_label(url: str) -> str:
    """Return the canonical hostname[:non-default-port] URL identity.

    Hostnames are normalized to their lowercase ASCII IDNA form. Equivalent
    explicit default ports share an identity, while non-default ports remain
    separate origins for rate limiting and circuit isolation.
    """
    parsed = urlparse(url)
    raw_host = parsed.hostname
    if raw_host is None:
        return "unknown-host"
    host = _canonical_hostname(raw_host)
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        host = str(literal)
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    scheme = parsed.scheme.lower()
    if port is None or (scheme, port) in {("http", 80), ("https", 443)}:
        return host
    return f"{host}:{port}"
