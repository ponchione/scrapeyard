from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from scripts.augment_browser_sbom import augment
from scripts.evaluate_container_scan import evaluate


def _report(severity: str = "HIGH"):
    return {
        "Results": [
            {
                "Target": "scrapeyard:0.7.0",
                "Class": "os-pkgs",
                "Vulnerabilities": [
                    {
                        "VulnerabilityID": "CVE-2099-0001",
                        "PkgName": "example",
                        "InstalledVersion": "1.0",
                        "Severity": severity,
                    }
                ],
            }
        ]
    }


def _policy(exceptions=None):
    return {
        "schema_version": 1,
        "maximum_acceptance_days": 30,
        "exceptions": exceptions or [],
    }


def test_unfixed_high_findings_are_not_implicitly_ignored():
    accepted, unaccepted = evaluate(
        [_report()],
        _policy(),
        today=date(2026, 7, 16),
    )

    assert accepted == []
    assert unaccepted[0]["id"] == "CVE-2099-0001"


def test_narrow_owner_assigned_exception_is_bounded():
    exception = {
        "kind": "vulnerability",
        "id": "CVE-2099-0001",
        "package": "example",
        "installed_version": "1.0",
        "target": "scrapeyard:0.7.0",
        "severity": "MEDIUM",
        "owner": "Scrapeyard maintainers",
        "rationale": "No fixed upstream build exists during the bounded review window.",
        "compensating_controls": ["The affected feature is unreachable."],
        "accepted_on": "2026-07-16",
        "expires_on": "2026-07-30",
    }

    accepted, unaccepted = evaluate(
        [_report("MEDIUM")],
        _policy([exception]),
        today=date(2026, 7, 16),
    )

    assert len(accepted) == 1
    assert unaccepted == []


@pytest.mark.parametrize(
    "update",
    [
        {"owner": ""},
        {"rationale": ""},
        {"compensating_controls": []},
        {"expires_on": "2026-09-01"},
        {"expires_on": "2026-07-15"},
    ],
)
def test_ungoverned_or_expired_exceptions_fail(update):
    entry = {
        "kind": "vulnerability",
        "id": "CVE-2099-0001",
        "package": "example",
        "installed_version": "1.0",
        "target": "scrapeyard:0.7.0",
        "severity": "MEDIUM",
        "owner": "Scrapeyard maintainers",
        "rationale": "Bounded operational acceptance.",
        "compensating_controls": ["Affected code is disabled."],
        "accepted_on": "2026-07-16",
        "expires_on": "2026-07-30",
        **update,
    }

    with pytest.raises(ValueError):
        evaluate([_report()], _policy([entry]), today=date(2026, 7, 16))


def test_browser_binaries_are_explicit_sbom_components():
    sbom = {"bomFormat": "CycloneDX", "components": []}
    runtime = {
        "browser_binaries": [
            {
                "name": "chromium",
                "version": "149.0.7827.55",
                "revision": "1228",
                "path": "/ms-playwright/chrome",
                "reported_version": "Chromium 149.0.7827.55",
                "consumers": ["playwright", "patchright"],
            },
            {
                "name": "camoufox",
                "version": "150.0.2",
                "release": "beta.25",
                "path": "/opt/camoufox/camoufox-bin",
                "reported_version": "Mozilla Firefox 150.0.2",
                "consumers": ["cloverlabs-camoufox"],
            },
        ]
    }

    augmented = augment(sbom, runtime)

    assert {component["version"] for component in augmented["components"]} == {
        "149.0.7827.55",
        "150.0.2",
    }


@pytest.mark.parametrize("severity", ["HIGH", "CRITICAL"])
def test_high_and_critical_findings_cannot_be_excepted(severity):
    exception = {
        "kind": "vulnerability",
        "id": "CVE-2099-0001",
        "package": "example",
        "installed_version": "1.0",
        "target": "scrapeyard:0.7.0",
        "severity": severity,
        "owner": "Scrapeyard maintainers",
        "rationale": "No fixed upstream build exists.",
        "compensating_controls": ["Affected code is disabled."],
        "accepted_on": "2026-07-16",
        "expires_on": "2026-07-30",
    }

    with pytest.raises(ValueError, match="only Medium"):
        evaluate([_report(severity)], _policy([exception]), today=date(2026, 7, 16))


def test_exact_affected_package_group_is_supported():
    exception = {
        "kind": "vulnerability",
        "id": "CVE-2099-0001",
        "affected_packages": [
            {"package": "example", "installed_version": "1.0"},
            {"package": "example-library", "installed_version": "1.0"},
        ],
        "target": "scrapeyard:0.7.0",
        "severity": "MEDIUM",
        "owner": "Scrapeyard maintainers",
        "rationale": "One source package produces two exact binary packages.",
        "compensating_controls": ["Affected code is disabled."],
        "accepted_on": "2026-07-16",
        "expires_on": "2026-07-30",
    }

    accepted, unaccepted = evaluate(
        [_report("MEDIUM")], _policy([exception]), today=date(2026, 7, 16)
    )

    assert len(accepted) == 1
    assert unaccepted == []


def test_checked_in_container_acceptances_are_current_and_medium_only():
    policy = json.loads(
        Path("security/container-scan-exceptions.json").read_text(encoding="utf-8")
    )

    accepted, unaccepted = evaluate([], policy, today=date(2026, 7, 16))

    assert accepted == []
    assert unaccepted == []
    assert policy["exceptions"]
    assert {entry["severity"] for entry in policy["exceptions"]} == {"MEDIUM"}
    assert {entry["target_scope"] for entry in policy["exceptions"]} == {
        "ubuntu 24.04"
    }
