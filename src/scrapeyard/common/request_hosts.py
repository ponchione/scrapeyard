"""The names a deployment answers to, and the name a request says it is for.

Both sides are reduced to the same form, lower case with no port and no
trailing dot, an address in its compressed form, so that one comparison
decides.
"""

from __future__ import annotations

import ipaddress
import re

_LABEL = r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?"
_HOSTNAME_RE = re.compile(rf"{_LABEL}(?:\.{_LABEL})*")
_PORT_RE = re.compile(r"[0-9]{1,5}")


def _normalize(host: str) -> str | None:
    if host.endswith("."):
        host = host[:-1]
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        pass
    if len(host) > 253 or _HOSTNAME_RE.fullmatch(host) is None:
        return None
    return host


def parse_allowed_hosts(value: str) -> frozenset[str]:
    """Read a comma-separated list of host names and addresses, without ports."""

    hosts: set[str] = set()
    for entry in (part.strip().lower() for part in value.split(",")):
        if not entry:
            continue
        if entry.startswith("[") and entry.endswith("]"):
            entry = entry[1:-1]
        host = _normalize(entry)
        if host is None:
            raise ValueError(
                "SCRAPEYARD_ALLOWED_HOSTS entries must be host names or addresses, "
                "with no port, scheme, path or wildcard"
            )
        hosts.add(host)
    return frozenset(hosts)


def request_host(header: bytes) -> str | None:
    """The host a Host header names, in the allowed list's form; None if malformed."""

    try:
        text = header.decode("ascii").strip().lower()
    except UnicodeDecodeError:
        return None
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            return None
        rest = text[end + 1 :]
        if rest and not (rest[0] == ":" and _PORT_RE.fullmatch(rest[1:])):
            return None
        try:
            return ipaddress.IPv6Address(text[1:end]).compressed
        except ValueError:
            return None
    host, colon, port = text.partition(":")
    if colon and _PORT_RE.fullmatch(port) is None:
        return None
    return _normalize(host)
