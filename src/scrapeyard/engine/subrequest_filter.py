"""Opt-in browser subrequest blocking: third-party hosts and URL patterns."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from scrapeyard.common.traffic import registrable_domain, url_hostname, url_site


def glob_matches(parts: tuple[str, ...], text: str) -> bool:
    """Match a ``*``-only glob, split on ``*``, against all of *text*.

    Every other character is literal. Middle parts are found left to right, so
    a match never backtracks.
    """
    if len(parts) == 1:
        return text == parts[0]
    first, last = parts[0], parts[-1]
    if (
        len(text) < len(first) + len(last)
        or not text.startswith(first)
        or not text.endswith(last)
    ):
        return False
    position, end = len(first), len(text) - len(last)
    for part in parts[1:-1]:
        index = text.find(part, position, end)
        if index < 0:
            return False
        position = index + len(part)
    return True


@dataclass(frozen=True)
class SubrequestFilter:
    """Decide which browser subrequests of one target are aborted before sending."""

    site: str | None
    block_third_party: bool
    allow_hosts: tuple[str, ...]
    url_patterns: tuple[tuple[str, ...], ...]

    @classmethod
    def for_target(cls, browser: Any, target_url: str) -> SubrequestFilter | None:
        """Build the target's filter, or ``None`` when no blocking is configured."""
        if not browser.block_third_party and not browser.block_url_patterns:
            return None
        return cls(
            site=url_site(target_url),
            block_third_party=browser.block_third_party,
            allow_hosts=tuple(browser.third_party_allow_hosts),
            url_patterns=tuple(tuple(pattern.split("*")) for pattern in browser.block_url_patterns),
        )

    def _allowed(self, hostname: str) -> bool:
        return any(
            hostname == host or hostname.endswith(f".{host}") for host in self.allow_hosts
        )

    def blocks(self, url: str) -> bool:
        if self.block_third_party and self.site is not None:
            hostname = url_hostname(url)
            if (
                hostname is not None
                and registrable_domain(hostname) != self.site
                and not self._allowed(hostname)
            ):
                return True
        return any(glob_matches(parts, url) for parts in self.url_patterns)
