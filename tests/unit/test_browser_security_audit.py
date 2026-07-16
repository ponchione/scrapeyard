from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from scripts.audit_browser_security import BrowserAuditError, audit, load_policy


ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "security" / "browser-policy.json"


def _runtime_manifest(tmp_path: Path) -> Path:
    policy = load_policy(POLICY)
    manifest = {
        "schema_version": 1,
        "components": {
            name: {"version": component["current"]}
            for name, component in policy["components"].items()
        },
        "browser_binaries": [
            {
                "name": "chromium",
                "path": "/ms-playwright/chromium/chrome",
                "reported_version": "Chromium 149.0.7827.55",
            },
            {
                "name": "camoufox",
                "path": "/opt/camoufox/camoufox-bin",
                "reported_version": "Mozilla Firefox 150.0.2",
            },
        ],
    }
    path = tmp_path / "browser-runtime.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_checked_in_browser_policy_matches_the_dockerfile(tmp_path):
    audit(
        POLICY,
        dockerfile=ROOT / "Dockerfile",
        manifest=_runtime_manifest(tmp_path),
        as_of=date(2026, 7, 16),
    )


@pytest.mark.parametrize(
    ("component", "vulnerable_version"),
    [
        ("camoufox-browser", "135.0.1"),
        ("chromium", "136.0.7103.25"),
    ],
)
def test_known_vulnerable_browser_binaries_fail_the_audit(
    tmp_path,
    component,
    vulnerable_version,
):
    manifest_path = _runtime_manifest(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["components"][component]["version"] = vulnerable_version
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BrowserAuditError, match="below"):
        audit(
            POLICY,
            dockerfile=None,
            manifest=manifest_path,
            as_of=date(2026, 7, 16),
        )


def test_expired_browser_review_fails_closed(tmp_path):
    with pytest.raises(BrowserAuditError, match="expired"):
        audit(
            POLICY,
            dockerfile=None,
            manifest=_runtime_manifest(tmp_path),
            as_of=date(2026, 8, 16),
        )
