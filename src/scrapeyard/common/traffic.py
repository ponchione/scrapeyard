"""Per-host and per-resource-type traffic counts reported in ``run_budget.traffic``."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

from tldextract import TLDExtract

# The dependency's bundled Public Suffix List snapshot, never a runtime download.
OFFLINE_TLD_EXTRACTOR = TLDExtract(cache_dir=None, suffix_list_urls=())

# Hosts listed in the report; the totals always cover every host.
REPORTED_HOSTS = 25
# Distinct hosts tracked per run; later hosts are pooled per party.
MAX_TRACKED_HOSTS = 1000
_OTHER_HOSTS = {False: "(other first-party hosts)", True: "(other third-party hosts)"}
# Browser resource types counted separately; every other type counts as "other".
RESOURCE_TYPES = ("document", "script", "xhr", "fetch", "other")
_NAMED_TYPES = frozenset(RESOURCE_TYPES)


@lru_cache(maxsize=4096)
def registrable_domain(hostname: str) -> str:
    """Return the registrable domain (public suffix plus one label) of *hostname*.

    IP addresses are their own domain. Names under a suffix the bundled list does
    not know (such as reserved ``.test`` names) use their last two labels.
    """
    host = hostname.lower().rstrip(".")
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        return host
    parts = OFFLINE_TLD_EXTRACTOR(host)
    if parts.domain and parts.suffix:
        return f"{parts.domain}.{parts.suffix}"
    return ".".join(host.rsplit(".", 2)[-2:])


def url_hostname(url: str) -> str | None:
    """Return the lowercase hostname of *url* without port or userinfo."""
    if not isinstance(url, str):
        return None
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


def url_site(url: str) -> str | None:
    """Return the registrable domain of *url*'s host, or ``None`` without a host."""
    hostname = url_hostname(url)
    return registrable_domain(hostname) if hostname else None


@dataclass
class _Counts:
    requests: int = 0
    blocked: int = 0
    bytes: int = 0

    def report(self) -> dict[str, int]:
        return {"requests": self.requests, "blocked": self.blocked, "bytes": self.bytes}


@dataclass
class _HostCounts(_Counts):
    third_party: bool = False
    types: dict[str, _Counts] = field(default_factory=dict)

    def of_type(self, resource_type: str) -> _Counts:
        name = resource_type if resource_type in _NAMED_TYPES else "other"
        counts = self.types.get(name)
        if counts is None:
            counts = self.types[name] = _Counts()
        return counts


class HostTraffic:
    """Bounded per-host counters for one run; only hostnames are retained.

    ``requests`` are requests released to the network (including native
    redirect hops), ``blocked`` are browser requests aborted before sending, and
    ``bytes`` are response bytes the transport reported receiving. Each count is
    also kept per resource type (:data:`RESOURCE_TYPES`). A host is third-party
    when its registrable domain differs from the requesting target's; a host
    that is first-party for any target stays first-party.
    """

    def __init__(self, *, max_hosts: int = MAX_TRACKED_HOSTS) -> None:
        self._max_hosts = max_hosts
        self._hosts: dict[str, _HostCounts] = {}

    def _counts(self, url: str, site: str | None) -> _HostCounts:
        hostname = url_hostname(url) or "unknown-host"
        third_party = site is not None and registrable_domain(hostname) != site
        counts = self._hosts.get(hostname)
        if counts is None:
            if len(self._hosts) >= self._max_hosts:
                hostname = _OTHER_HOSTS[third_party]
                counts = self._hosts.get(hostname)
            if counts is None:
                counts = self._hosts[hostname] = _HostCounts(third_party=third_party)
        elif not third_party:
            counts.third_party = False
        return counts

    def request(self, url: str, site: str | None, resource_type: str = "other") -> None:
        counts = self._counts(url, site)
        counts.requests += 1
        counts.of_type(resource_type).requests += 1

    def blocked(self, url: str, site: str | None, resource_type: str = "other") -> None:
        counts = self._counts(url, site)
        counts.blocked += 1
        counts.of_type(resource_type).blocked += 1

    def received(
        self, url: str, site: str | None, amount: int, resource_type: str = "other",
    ) -> None:
        if amount > 0:
            counts = self._counts(url, site)
            counts.bytes += amount
            counts.of_type(resource_type).bytes += amount

    def snapshot(self, *, reported_hosts: int = REPORTED_HOSTS) -> dict[str, Any]:
        ranked = sorted(
            self._hosts.items(),
            key=lambda item: (-item[1].requests, -item[1].bytes, -item[1].blocked, item[0]),
        )

        def totals(third_party: bool) -> dict[str, int]:
            group = [counts for _, counts in ranked if counts.third_party is third_party]
            return {
                "hosts": len(group),
                "requests": sum(counts.requests for counts in group),
                "blocked": sum(counts.blocked for counts in group),
                "bytes": sum(counts.bytes for counts in group),
            }

        by_type = {name: _Counts() for name in RESOURCE_TYPES}
        for _, counts in ranked:
            for name, typed in counts.types.items():
                total = by_type[name]
                total.requests += typed.requests
                total.blocked += typed.blocked
                total.bytes += typed.bytes

        first, third = totals(False), totals(True)
        return {
            "requests": first["requests"] + third["requests"],
            "blocked": first["blocked"] + third["blocked"],
            "bytes": first["bytes"] + third["bytes"],
            "first_party": first,
            "third_party": third,
            "resource_types": {name: counts.report() for name, counts in by_type.items()},
            "hosts": [
                {
                    "host": host,
                    "third_party": counts.third_party,
                    **counts.report(),
                    "resource_types": {
                        name: counts.types[name].report()
                        for name in RESOURCE_TYPES
                        if name in counts.types
                    },
                }
                for host, counts in ranked[:reported_hosts]
            ],
            "hosts_omitted": max(0, len(ranked) - reported_hosts),
        }
