from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from scrapeyard.api.auth import (
    ALL_AUTH_SCOPES,
    AuthScope,
    authenticate_api_key,
    parse_api_credentials,
    validate_auth_configuration,
)


def _raw_credentials() -> str:
    return json.dumps(
        {
            "eyebox-old": {
                "identity": "eyebox",
                "secret": "old-secret-000000000000",
                "scopes": ["submit", "read"],
                "projects": ["alpha"],
            },
            "eyebox-new": {
                "identity": "eyebox",
                "secret": "new-secret-000000000000",
                "scopes": ["submit", "read"],
                "projects": ["alpha"],
            },
        }
    )


def test_named_credentials_preserve_identity_across_overlapping_rotation():
    credentials = parse_api_credentials(_raw_credentials())

    old = authenticate_api_key("old-secret-000000000000", credentials)
    new = authenticate_api_key("new-secret-000000000000", credentials)

    assert old is not None and new is not None
    assert old.identity == new.identity == "eyebox"
    assert old.credential_name == "eyebox-old"
    assert new.credential_name == "eyebox-new"
    assert old.projects == new.projects == frozenset({"alpha"})


def test_authentication_compares_every_secret_for_valid_and_invalid_inputs():
    credentials = parse_api_credentials(_raw_credentials())

    with patch(
        "scrapeyard.api.auth.secrets.compare_digest",
        wraps=__import__("secrets").compare_digest,
    ) as compare:
        assert authenticate_api_key("old-secret-000000000000", credentials) is not None
        assert compare.call_count == len(credentials)
        compare.reset_mock()
        assert authenticate_api_key("invalid-secret-00000000", credentials) is None
        assert compare.call_count == len(credentials)


def test_revocation_removes_only_revoked_secret_without_identity_ambiguity():
    parsed = json.loads(_raw_credentials())
    parsed.pop("eyebox-old")
    credentials = parse_api_credentials(json.dumps(parsed))

    assert authenticate_api_key("old-secret-000000000000", credentials) is None
    caller = authenticate_api_key("new-secret-000000000000", credentials)
    assert caller is not None
    assert caller.identity == "eyebox"


@pytest.mark.parametrize(
    "raw,match",
    [
        ("[]", "JSON object"),
        (json.dumps({"bad name": {}}), "credential name"),
        (
            json.dumps(
                {
                    "test": {
                        "secret": "too-short",
                        "scopes": ["read"],
                    }
                }
            ),
            "16 to 512",
        ),
        (
            json.dumps(
                {
                    "test": {
                        "secret": "long-enough-secret",
                        "scopes": ["root"],
                    }
                }
            ),
            "invalid scope",
        ),
    ],
)
def test_malformed_named_credentials_fail_closed(raw, match):
    with pytest.raises(ValueError, match=match):
        parse_api_credentials(raw)


def test_legacy_key_migration_has_full_scope_but_named_format_is_primary():
    credentials = parse_api_credentials("", legacy_keys={"legacy-key"})
    caller = authenticate_api_key("legacy-key", credentials)
    assert caller is not None
    assert caller.scopes == ALL_AUTH_SCOPES
    assert AuthScope.health_detail in caller.scopes


def _production_credentials() -> tuple[str, str]:
    probe_key = "health-probe-key-000000000000"
    return (
        json.dumps(
            {
                "operator": {
                    "secret": "operator-key-000000000000000",
                    "scopes": ["submit", "read", "schedule-admin", "delete"],
                },
                "health-probe": {
                    "secret": probe_key,
                    "scopes": ["health-detail"],
                },
            }
        ),
        probe_key,
    )


def test_missing_credentials_fail_closed_by_default():
    with pytest.raises(ValueError, match="API credentials are required"):
        validate_auth_configuration(
            raw_credentials="",
            legacy_keys=set(),
            local_development_unauthenticated=False,
            health_probe_api_key="",
        )


def test_explicit_local_development_mode_allows_empty_credentials_only():
    assert validate_auth_configuration(
        raw_credentials="",
        legacy_keys=set(),
        local_development_unauthenticated=True,
        health_probe_api_key="",
    ) == ()

    raw, probe_key = _production_credentials()
    with pytest.raises(ValueError, match="cannot be enabled"):
        validate_auth_configuration(
            raw_credentials=raw,
            legacy_keys=set(),
            local_development_unauthenticated=True,
            health_probe_api_key=probe_key,
        )


def test_valid_production_credentials_require_a_dedicated_health_identity():
    raw, probe_key = _production_credentials()
    credentials = validate_auth_configuration(
        raw_credentials=raw,
        legacy_keys=set(),
        local_development_unauthenticated=False,
        health_probe_api_key=probe_key,
    )

    assert {credential.name for credential in credentials} == {
        "operator",
        "health-probe",
    }


@pytest.mark.parametrize(
    ("probe_key", "match"),
    [
        ("", "HEALTH_PROBE_API_KEY is required"),
        ("unconfigured-probe-key-000000", "must match"),
        ("operator-key-000000000000000", "only the health-detail scope"),
    ],
)
def test_invalid_health_probe_credentials_fail_startup(probe_key, match):
    raw, _valid_probe = _production_credentials()

    with pytest.raises(ValueError, match=match):
        validate_auth_configuration(
            raw_credentials=raw,
            legacy_keys=set(),
            local_development_unauthenticated=False,
            health_probe_api_key=probe_key,
        )


def test_project_restricted_health_probe_is_rejected():
    probe_key = "health-probe-key-000000000000"
    raw = json.dumps(
        {
            "health-probe": {
                "secret": probe_key,
                "scopes": ["health-detail"],
                "projects": ["alpha"],
            }
        }
    )

    with pytest.raises(ValueError, match="no project restriction"):
        validate_auth_configuration(
            raw_credentials=raw,
            legacy_keys=set(),
            local_development_unauthenticated=False,
            health_probe_api_key=probe_key,
        )
