"""Connected-destination egress policy attestation."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import errno
import logging
import secrets

from scrapeyard.common.settings import ServiceSettings


logger = logging.getLogger(__name__)


class EgressPolicyAttestationError(RuntimeError):
    """Raised when a controlled non-public destination remains reachable."""


_PROBE_REQUEST = "SCRAPEYARD-EGRESS-PROBE/1"
_PROBE_RESPONSE = "SCRAPEYARD-EGRESS-LIVE/1"


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with suppress(ConnectionError, OSError):
        await writer.wait_closed()


async def _attest_probe_liveness(settings: ServiceSettings) -> None:
    """Verify the controlled helper and its challenge listener are live."""

    nonce = secrets.token_hex(16)
    timeout = settings.egress_policy_probe_timeout_seconds
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                settings.egress_policy_probe_host,
                settings.egress_policy_probe_liveness_port,
            ),
            timeout=timeout,
        )
        try:
            writer.write(f"{_PROBE_REQUEST} {nonce}\n".encode())
            await asyncio.wait_for(writer.drain(), timeout=timeout)
            response = await asyncio.wait_for(reader.readline(), timeout=timeout)
        finally:
            await _close_writer(writer)
    except (TimeoutError, OSError, ValueError) as exc:
        raise EgressPolicyAttestationError(
            "Connected-IP egress policy could not be attested because controlled "
            "probe liveness could not be verified"
        ) from exc

    expected = (
        f"{_PROBE_RESPONSE} {nonce} {settings.egress_policy_probe_port}\n".encode()
    )
    if response != expected:
        raise EgressPolicyAttestationError(
            "Connected-IP egress policy could not be attested because controlled "
            "probe liveness returned an invalid response"
        )


async def attest_connected_ip_policy(settings: ServiceSettings) -> None:
    """Require a live controlled helper around one denied challenge attempt."""

    host = getattr(settings, "egress_policy_probe_host", "")
    port = getattr(settings, "egress_policy_probe_port", 0)
    if not host or not port:
        return
    await _attest_probe_liveness(settings)
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=settings.egress_policy_probe_timeout_seconds,
        )
    except TimeoutError:
        pass
    except OSError as exc:
        denied_errors = {
            errno.EACCES,
            errno.ECONNREFUSED,
            errno.EHOSTUNREACH,
            errno.ENETUNREACH,
            errno.ETIMEDOUT,
        }
        if exc.errno not in denied_errors:
            raise EgressPolicyAttestationError(
                "Connected-IP egress policy could not be attested because the "
                "controlled challenge failed unexpectedly"
            ) from exc
    else:
        await _close_writer(writer)
        raise EgressPolicyAttestationError(
            "Connected-IP egress policy attestation failed: controlled non-public "
            "challenge was reachable"
        )

    await _attest_probe_liveness(settings)
    logger.info(
        "Connected-IP egress policy attested probe_host=%s probe_port=%s "
        "probe_liveness_port=%s",
        host,
        port,
        settings.egress_policy_probe_liveness_port,
    )
