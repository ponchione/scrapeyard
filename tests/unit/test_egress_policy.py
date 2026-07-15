from __future__ import annotations

import asyncio
from contextlib import suppress
import errno
import socket

import pytest

from scrapeyard.common.settings import ServiceSettings
from scrapeyard.runtime.egress import (
    EgressPolicyAttestationError,
    attest_connected_ip_policy,
)


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with suppress(ConnectionError, OSError):
        await writer.wait_closed()


async def _start_controlled_probe(
) -> tuple[asyncio.Server, asyncio.Server, int, int]:
    async def challenge(_reader, writer):
        await _close_writer(writer)

    challenge_server = await asyncio.start_server(challenge, "127.0.0.1", 0)
    challenge_port = challenge_server.sockets[0].getsockname()[1]

    async def liveness(reader, writer):
        try:
            request = (await reader.readline()).decode().rstrip("\n")
            prefix, nonce = request.split(" ", 1)
            if prefix == "SCRAPEYARD-EGRESS-PROBE/1" and challenge_server.is_serving():
                writer.write(
                    f"SCRAPEYARD-EGRESS-LIVE/1 {nonce} {challenge_port}\n".encode()
                )
                await writer.drain()
        finally:
            await _close_writer(writer)

    liveness_server = await asyncio.start_server(liveness, "127.0.0.1", 0)
    liveness_port = liveness_server.sockets[0].getsockname()[1]
    return challenge_server, liveness_server, challenge_port, liveness_port


def _settings(challenge_port: int, liveness_port: int) -> ServiceSettings:
    return ServiceSettings(
        egress_policy_probe_host="127.0.0.1",
        egress_policy_probe_port=challenge_port,
        egress_policy_probe_liveness_port=liveness_port,
        egress_policy_probe_timeout_seconds=0.2,
    )


async def _stop_servers(*servers: asyncio.Server) -> None:
    for server in servers:
        server.close()
    await asyncio.gather(*(server.wait_closed() for server in servers))


@pytest.mark.asyncio
async def test_egress_attestation_rejects_reachable_live_challenge():
    challenge, liveness, challenge_port, liveness_port = (
        await _start_controlled_probe()
    )
    try:
        with pytest.raises(EgressPolicyAttestationError, match="challenge was reachable"):
            await attest_connected_ip_policy(_settings(challenge_port, liveness_port))
    finally:
        await _stop_servers(challenge, liveness)


@pytest.mark.asyncio
async def test_egress_attestation_rejects_unused_closed_probe_ports():
    with socket.socket() as unused_challenge, socket.socket() as unused_liveness:
        unused_challenge.bind(("127.0.0.1", 0))
        unused_liveness.bind(("127.0.0.1", 0))
        challenge_port = unused_challenge.getsockname()[1]
        liveness_port = unused_liveness.getsockname()[1]

    with pytest.raises(EgressPolicyAttestationError, match="liveness could not be verified"):
        await attest_connected_ip_policy(_settings(challenge_port, liveness_port))


@pytest.mark.asyncio
async def test_egress_attestation_accepts_live_probe_with_blocked_challenge(monkeypatch):
    challenge, liveness, challenge_port, liveness_port = (
        await _start_controlled_probe()
    )
    real_open_connection = asyncio.open_connection

    async def open_with_policy(host, port, *args, **kwargs):
        if port == challenge_port:
            raise OSError(errno.EACCES, "blocked by policy")
        return await real_open_connection(host, port, *args, **kwargs)

    monkeypatch.setattr(asyncio, "open_connection", open_with_policy)
    try:
        await attest_connected_ip_policy(_settings(challenge_port, liveness_port))
    finally:
        await _stop_servers(challenge, liveness)


@pytest.mark.asyncio
async def test_egress_attestation_fails_when_probe_dies_during_attestation(monkeypatch):
    challenge, liveness, challenge_port, liveness_port = (
        await _start_controlled_probe()
    )
    real_open_connection = asyncio.open_connection

    async def die_after_initial_liveness(host, port, *args, **kwargs):
        if port == challenge_port:
            challenge.close()
            liveness.close()
            raise OSError(errno.EACCES, "blocked by policy")
        return await real_open_connection(host, port, *args, **kwargs)

    monkeypatch.setattr(asyncio, "open_connection", die_after_initial_liveness)
    try:
        with pytest.raises(EgressPolicyAttestationError, match="liveness could not be verified"):
            await attest_connected_ip_policy(_settings(challenge_port, liveness_port))
    finally:
        await _stop_servers(challenge, liveness)


@pytest.mark.asyncio
async def test_egress_attestation_fails_closed_on_unexpected_challenge_error(monkeypatch):
    challenge, liveness, challenge_port, liveness_port = (
        await _start_controlled_probe()
    )
    real_open_connection = asyncio.open_connection

    async def fail_challenge(host, port, *args, **kwargs):
        if port == challenge_port:
            raise OSError(errno.EMFILE, "too many open files")
        return await real_open_connection(host, port, *args, **kwargs)

    monkeypatch.setattr(asyncio, "open_connection", fail_challenge)
    try:
        with pytest.raises(EgressPolicyAttestationError, match="failed unexpectedly"):
            await attest_connected_ip_policy(_settings(challenge_port, liveness_port))
    finally:
        await _stop_servers(challenge, liveness)


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
            {
                "egress_policy_probe_host": "10.0.0.2",
                "egress_policy_probe_port": 80,
            },
            "must be configured together",
        ),
        (
            {
                "egress_policy_probe_host": "probe.internal",
                "egress_policy_probe_port": 80,
                "egress_policy_probe_liveness_port": 81,
            },
            "must be an IP address",
        ),
        (
            {
                "egress_policy_probe_host": "8.8.8.8",
                "egress_policy_probe_port": 80,
                "egress_policy_probe_liveness_port": 81,
            },
            "must be a controlled non-public unicast address",
        ),
        (
            {
                "egress_policy_probe_host": "10.0.0.2",
                "egress_policy_probe_port": 80,
                "egress_policy_probe_liveness_port": 80,
            },
            "challenge and liveness ports must be different",
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
        egress_policy_probe_liveness_port=8081,
    )

    assert settings.untrusted_submissions is True
    assert settings.proxy_url == "http://8.8.8.8:8080"
