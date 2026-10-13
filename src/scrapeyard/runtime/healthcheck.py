"""The container's health check: ask this instance, from inside, whether it is ready.

Docker runs it in the application's container, which is where the credentials
are. It asks `/health/ready` with a credential that holds `health-detail` and
is healthy on a 200, which the application gives when every dependency and
background task is in order (degraded capacity included) and withholds when
one is not. A deployment with no such credential has nothing to ask
readiness with, and is checked for liveness as before.

It imports the standard library only: it starts every half minute, and the
application's own imports would cost each run more than the answer does.
"""

from __future__ import annotations

import http.client
import json
import os
import sys
from collections.abc import Mapping

_SCOPE = "health-detail"
# Docker gives the check ten seconds; answer before it gives up on us.
_TIMEOUT_SECONDS = 8.0


def readiness_key(environment: Mapping[str, str]) -> str | None:
    """A secret this deployment accepts for `/health/ready`, or None.

    Reads what the application reads, less strictly: a credential it would
    refuse at startup is not this check's to report, since an application
    that refused its settings is not running to be asked.
    """
    try:
        named = json.loads(environment.get("SCRAPEYARD_API_CREDENTIALS") or "{}")
    except ValueError:
        named = {}
    if isinstance(named, dict):
        for spec in named.values():
            if not isinstance(spec, dict):
                continue
            secret, scopes = spec.get("secret"), spec.get("scopes")
            if isinstance(secret, str) and isinstance(scopes, list) and _SCOPE in scopes:
                return secret
    # Keys from before named credentials hold every scope.
    for legacy in (environment.get("SCRAPEYARD_API_KEYS") or "").split(","):
        if legacy.strip():
            return legacy.strip()
    return None


def check(environment: Mapping[str, str], *, host: str = "127.0.0.1", port: int = 8420) -> bool:
    key = readiness_key(environment)
    connection = http.client.HTTPConnection(host, port, timeout=_TIMEOUT_SECONDS)
    try:
        if key is None:
            connection.request("GET", "/health/live")
        else:
            connection.request("GET", "/health/ready", headers={"X-API-Key": key})
        return connection.getresponse().status == 200
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def main() -> int:
    return 0 if check(os.environ) else 1


if __name__ == "__main__":
    sys.exit(main())
