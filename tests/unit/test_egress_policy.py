from __future__ import annotations

import asyncio
import errno
import socket

import pytest

from scrapeyard.common.settings import ServiceSettings
from scrapeyard.runtime.egress import (
    EgressPolicyAttestationError,
    attest_connected_ip_policy,
)


@pytest.mark.asyncio
async def test_egress_attestation_rejects_reachable_controlled_destination():
    async def close_connection(_reader, writer):
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(close_connection, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    settings = ServiceSettings(
        egress_policy_probe_host="127.0.0.1",
        egress_policy_probe_port=port,
        egress_policy_probe_timeout_seconds=0.2,
    )
    try:
        with pytest.raises(EgressPolicyAttestationError, match="probe was reachable"):
            await attest_connected_ip_policy(settings)
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_egress_attestation_accepts_denied_controlled_destination():
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        port = unused.getsockname()[1]
    settings = ServiceSettings(
        egress_policy_probe_host="127.0.0.1",
        egress_policy_probe_port=port,
        egress_policy_probe_timeout_seconds=0.2,
    )

    await attest_connected_ip_policy(settings)


@pytest.mark.asyncio
async def test_egress_attestation_fails_closed_on_unexpected_socket_error(monkeypatch):
    async def fail_to_open(*_args, **_kwargs):
        raise OSError(errno.EMFILE, "too many open files")

    monkeypatch.setattr(asyncio, "open_connection", fail_to_open)
    settings = ServiceSettings(
        egress_policy_probe_host="127.0.0.1",
        egress_policy_probe_port=8080,
    )

    with pytest.raises(EgressPolicyAttestationError, match="failed unexpectedly"):
        await attest_connected_ip_policy(settings)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {"untrusted_submissions": True},
            "requires an egress-filtering operator proxy_url",
        ),
        (
            {"untrusted_submissions": True, "proxy_url": "direct"},
            "requires an egress-filtering operator proxy_url",
        ),
        (
            {
                "untrusted_submissions": True,
                "proxy_url": "http://8.8.8.8:8080",
            },
            "requires a connected-IP egress policy probe",
        ),
        (
            {"egress_policy_probe_host": "probe.internal", "egress_policy_probe_port": 80},
            "must be an IP address",
        ),
        (
            {"egress_policy_probe_host": "8.8.8.8", "egress_policy_probe_port": 80},
            "must be a controlled non-public unicast address",
        ),
    ],
)
def test_untrusted_submission_settings_fail_closed(kwargs, message):
    with pytest.raises(ValueError, match=message):
        ServiceSettings(**kwargs)


def test_untrusted_submission_settings_accept_complete_operator_boundary():
    settings = ServiceSettings(
        untrusted_submissions=True,
        proxy_url="http://8.8.8.8:8080",
        egress_policy_probe_host="10.0.0.2",
        egress_policy_probe_port=8080,
    )

    assert settings.untrusted_submissions is True
    assert settings.proxy_url == "http://8.8.8.8:8080"
