from __future__ import annotations

import ipaddress

import pytest

from scrapeyard.engine.ip_policy import (
    DENIED_ADDRESS_NETWORKS,
    PUBLIC_ADDRESS_EXCEPTIONS,
    firewall_policy_entries,
    ip_is_blocked,
)


def _firewall_blocks(value: str) -> bool:
    address = ipaddress.ip_address(value)
    for family, action, cidr in firewall_policy_entries():
        if int(family) != address.version:
            continue
        if address in ipaddress.ip_network(cidr):
            return action == "deny"
    return False


@pytest.mark.parametrize(
    ("value", "blocked"),
    [
        ("8.8.8.8", False),
        ("2606:4700:4700::1111", False),
        ("2001:1::1", False),
        ("10.0.0.1", True),
        ("100.64.0.1", True),
        ("169.254.169.254", True),
        ("192.0.2.1", True),
        ("198.51.100.1", True),
        ("203.0.113.1", True),
        ("224.0.0.1", True),
        ("::1", True),
        ("100::1", True),
        ("100:0:0:1::1", True),
        ("2001:db8::1", True),
        ("3fff::1", True),
        ("5f00::1", True),
        ("fc00::1", True),
        ("ff02::1", True),
        ("::ffff:8.8.8.8", False),
        ("::ffff:192.0.2.1", True),
        ("64:ff9b::808:808", False),
        ("64:ff9b::c000:201", True),
        ("64:ff9b:1::808:808", True),
        ("2002:0808:0808::1", True),
        ("2001::4136:e378:8000:63bf:3fff:fdd2", True),
    ],
)
def test_application_and_firewall_share_reviewed_connected_address_policy(
    value: str,
    blocked: bool,
) -> None:
    assert ip_is_blocked(ipaddress.ip_address(value)) is blocked
    assert _firewall_blocks(value) is blocked


@pytest.mark.parametrize("network", DENIED_ADDRESS_NETWORKS, ids=str)
def test_every_reviewed_direct_deny_network_is_enforced_by_both_consumers(
    network,
) -> None:
    value = str(network.network_address)

    assert ip_is_blocked(ipaddress.ip_address(value)) is True
    assert _firewall_blocks(value) is True


@pytest.mark.parametrize("network", PUBLIC_ADDRESS_EXCEPTIONS, ids=str)
def test_every_reviewed_public_exception_precedes_covering_denies(network) -> None:
    value = str(network.network_address)

    assert ip_is_blocked(ipaddress.ip_address(value)) is False
    assert _firewall_blocks(value) is False
