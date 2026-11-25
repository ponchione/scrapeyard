"""Stable connected-address policy shared by URL checks and host firewalls.

The reviewed ranges are based on the IANA IPv4 and IPv6 special-purpose
registries as updated 2025-10-09. They are intentionally fixed rather than
delegating the security boundary to Python-version-specific ``is_global``
tables. The policy is stricter for special-purpose blocks whose small public
exceptions are not useful scrape destinations (notably ``192.0.0.0/24``).
"""

from __future__ import annotations

import ipaddress

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def _networks(*values: str) -> tuple[IPNetwork, ...]:
    return tuple(ipaddress.ip_network(value) for value in values)


# Globally reachable exceptions inside the otherwise special-purpose
# 2001::/23 block. These rules must precede the deny rule in both consumers.
PUBLIC_ADDRESS_EXCEPTIONS: tuple[IPNetwork, ...] = _networks(
    "2001:1::1/128",
    "2001:1::2/128",
    "2001:1::3/128",
    "2001:3::/32",
    "2001:4:112::/48",
    "2001:20::/28",
    "2001:30::/28",
)

DENIED_ADDRESS_NETWORKS: tuple[IPNetwork, ...] = _networks(
    # IPv4 non-public, multicast, documentation, and reserved destinations.
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.0.2.0/24",
    "192.88.99.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "224.0.0.0/4",
    "240.0.0.0/4",
    # IPv6 special-purpose, transition, documentation, local, and multicast.
    # ::/96 is the unspecified and loopback addresses and the deprecated
    # IPv4-compatible form (::a.b.c.d); ::ffff:0:0:0/96 is IPv4-translated
    # (SIIT). Neither is a real destination, and both can spell an IPv4 one.
    "::/96",
    "::ffff:0:0:0/96",
    "64:ff9b:1::/48",
    "100::/64",
    "100:0:0:1::/64",
    "2001::/23",
    "2001:db8::/32",
    "2002::/16",
    "3fff::/20",
    "5f00::/16",
    "fc00::/7",
    "fe80::/10",
    # Site-local, deprecated by RFC 3879.
    "fec0::/10",
    "ff00::/8",
)

_IPV4_EMBEDDING_PREFIXES = (
    ipaddress.IPv6Network("64:ff9b::/96"),
    ipaddress.IPv6Network("64:ff9b:1::/48"),
)


def embedded_ipv4_addresses(address: ipaddress.IPv6Address) -> tuple[ipaddress.IPv4Address, ...]:
    """Return IPv4 endpoints encoded by supported IPv6 transition formats."""

    addresses: list[ipaddress.IPv4Address] = []
    if address.ipv4_mapped is not None:
        addresses.append(address.ipv4_mapped)
    if address.sixtofour is not None:
        addresses.append(address.sixtofour)
    if address.teredo is not None:
        addresses.extend(address.teredo)
    for prefix in _IPV4_EMBEDDING_PREFIXES:
        if address in prefix:
            addresses.append(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
    return tuple(addresses)


def ip_is_blocked(address: IPAddress) -> bool:
    """Return whether the reviewed policy rejects a connected destination."""

    if any(address in network for network in PUBLIC_ADDRESS_EXCEPTIONS):
        return False
    if any(address in network for network in DENIED_ADDRESS_NETWORKS):
        return True
    if isinstance(address, ipaddress.IPv6Address):
        return any(ip_is_blocked(embedded) for embedded in embedded_ipv4_addresses(address))
    return False
