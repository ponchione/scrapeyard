"""Submission-time authority boundary for caller-controlled transports."""

from __future__ import annotations

from scrapeyard.api.auth import AuthScope, AuthenticatedCaller
from scrapeyard.api.response_utils import raise_json_error
from scrapeyard.common.settings import ServiceSettings
from scrapeyard.config.schema import ScrapeConfig


def enforce_submission_transport_policy(
    config: ScrapeConfig,
    *,
    caller: AuthenticatedCaller,
    settings: ServiceSettings,
) -> None:
    """Reject caller-selected proxy/CDP transports in untrusted mode."""

    if not settings.untrusted_submissions:
        return
    has_override = config.proxy is not None or any(
        target.proxy is not None
        or (target.browser is not None and target.browser.cdp_url is not None)
        for target in config.resolved_targets()
    )
    if has_override and AuthScope.transport_admin not in caller.scopes:
        raise_json_error(
            403,
            "Caller lacks required scope 'transport-admin' for proxy or CDP overrides",
        )
