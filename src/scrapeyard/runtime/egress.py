"""Connected-destination egress policy attestation."""

from __future__ import annotations

import asyncio
import errno
import logging

from scrapeyard.common.settings import ServiceSettings


logger = logging.getLogger(__name__)


class EgressPolicyAttestationError(RuntimeError):
    """Raised when a controlled non-public destination remains reachable."""


async def attest_connected_ip_policy(settings: ServiceSettings) -> None:
    """Fail startup if the configured denied destination accepts a connection."""

    host = getattr(settings, "egress_policy_probe_host", "")
    port = getattr(settings, "egress_policy_probe_port", 0)
    if not host or not port:
        return
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=getattr(settings, "egress_policy_probe_timeout_seconds", 1.0),
        )
    except TimeoutError:
        logger.info(
            "Connected-IP egress policy attested probe_host=%s probe_port=%s",
            host,
            port,
        )
        return
    except OSError as exc:
        denied_errors = {
            errno.EACCES,
            errno.ECONNREFUSED,
            errno.EHOSTUNREACH,
            errno.ENETUNREACH,
            errno.ETIMEDOUT,
        }
        if exc.errno in denied_errors:
            logger.info(
                "Connected-IP egress policy attested probe_host=%s probe_port=%s",
                host,
                port,
            )
            return
        raise EgressPolicyAttestationError(
            "Connected-IP egress policy could not be attested because the "
            "controlled probe failed unexpectedly"
        ) from exc
    writer.close()
    raise EgressPolicyAttestationError(
        "Connected-IP egress policy attestation failed: controlled non-public "
        "probe was reachable"
    )
