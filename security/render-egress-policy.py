#!/usr/bin/env python3
"""Render the application address policy for the host firewall installer."""

from __future__ import annotations

import ipaddress
import os
import sys
from collections.abc import Iterable
from pathlib import Path

repository_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, os.fspath(repository_root / "src"))

from scrapeyard.engine.ip_policy import (  # noqa: E402
    DENIED_ADDRESS_NETWORKS,
    PUBLIC_ADDRESS_EXCEPTIONS,
)

_IPV4_DENIED_NETWORKS = tuple(
    network
    for network in DENIED_ADDRESS_NETWORKS
    if isinstance(network, ipaddress.IPv4Network)
)


def _embedded_ipv4_firewall_networks(
    prefix: ipaddress.IPv6Network,
    networks: Iterable[ipaddress.IPv4Network],
) -> tuple[ipaddress.IPv6Network, ...]:
    """Map IPv4 CIDRs into an IPv6 /96 prefix for ip6tables matching."""
    return tuple(
        ipaddress.IPv6Network(
            (int(prefix.network_address) | int(network.network_address), 96 + network.prefixlen)
        )
        for network in networks
    )


def firewall_policy_entries() -> tuple[tuple[str, str, str], ...]:
    """Render ordered ``(family, action, CIDR)`` host-firewall entries."""
    entries = [
        ("6" if network.version == 6 else "4", "allow", str(network))
        for network in PUBLIC_ADDRESS_EXCEPTIONS
    ]
    entries.extend(
        ("6" if network.version == 6 else "4", "deny", str(network))
        for network in DENIED_ADDRESS_NETWORKS
    )
    for prefix in (
        ipaddress.IPv6Network("::ffff:0:0/96"),
        ipaddress.IPv6Network("64:ff9b::/96"),
    ):
        entries.extend(
            ("6", "deny", str(network))
            for network in _embedded_ipv4_firewall_networks(prefix, _IPV4_DENIED_NETWORKS)
        )
    return tuple(entries)


if __name__ == "__main__":
    for family, action, cidr in firewall_policy_entries():
        print(f"{family}\t{action}\t{cidr}")
