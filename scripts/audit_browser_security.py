"""Enforce the checked-in browser security review and installed runtime versions."""

from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path
from typing import Any


class BrowserAuditError(RuntimeError):
    pass


def _version(value: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", value)
    if not parts:
        raise BrowserAuditError(f"Invalid version: {value!r}")
    return tuple(int(part) for part in parts)


def _at_least(actual: str, minimum: str) -> bool:
    left = _version(actual)
    right = _version(minimum)
    width = max(len(left), len(right))
    return left + (0,) * (width - len(left)) >= right + (0,) * (width - len(right))


def load_policy(path: Path) -> dict[str, Any]:
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BrowserAuditError(f"Unable to load browser policy {path}: {exc}") from exc
    if policy.get("schema_version") != 1 or not isinstance(policy.get("components"), dict):
        raise BrowserAuditError("Unsupported browser policy schema")
    return policy


def audit_review(policy: dict[str, Any], *, as_of: date) -> None:
    reviewed = date.fromisoformat(str(policy["reviewed_on"]))
    expires = date.fromisoformat(str(policy["expires_on"]))
    maximum_age = int(policy["maximum_review_age_days"])
    if expires < reviewed or (expires - reviewed).days > maximum_age:
        raise BrowserAuditError("Browser policy expiry exceeds its maximum review age")
    if as_of > expires:
        raise BrowserAuditError(
            f"Browser security review expired on {expires.isoformat()} (as of {as_of.isoformat()})"
        )


def audit_dockerfile(policy: dict[str, Any], dockerfile: Path) -> None:
    text = dockerfile.read_text(encoding="utf-8")
    arguments = dict(re.findall(r"(?m)^ARG ([A-Z0-9_]+)=\"?([^\"\s]+)\"?\s*$", text))
    for name, component in policy["components"].items():
        argument = component.get("docker_arg")
        if not argument:
            continue
        actual = arguments.get(argument)
        expected = str(component["current"])
        if actual != expected:
            raise BrowserAuditError(
                f"{name} Docker ARG {argument} is {actual!r}; policy requires {expected!r}"
            )
        if not _at_least(actual, str(component["minimum"])):
            raise BrowserAuditError(f"{name} {actual} is below the policy minimum")


def audit_manifest(policy: dict[str, Any], manifest_path: Path) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    observed = manifest.get("components", {})
    for name, component in policy["components"].items():
        actual_component = observed.get(name)
        if not isinstance(actual_component, dict):
            raise BrowserAuditError(f"Runtime manifest omitted {name}")
        actual = str(actual_component.get("version", ""))
        if not _at_least(actual, str(component["minimum"])):
            raise BrowserAuditError(
                f"Installed {name} {actual!r} is below {component['minimum']!r}"
            )
        if actual != str(component["current"]):
            raise BrowserAuditError(
                f"Installed {name} {actual!r} differs from reviewed {component['current']!r}"
            )
    binaries = manifest.get("browser_binaries")
    if not isinstance(binaries, list) or len(binaries) < 2:
        raise BrowserAuditError("Runtime manifest must contain Chromium and Camoufox binaries")
    for binary in binaries:
        if not binary.get("path") or not binary.get("reported_version"):
            raise BrowserAuditError("Runtime browser binary lacks path or executed version output")


def audit_rejected_versions(policy: dict[str, Any]) -> None:
    for rejected in policy.get("known_rejected_versions", []):
        component = policy["components"][rejected["component"]]
        if _at_least(str(rejected["version"]), str(component["minimum"])):
            raise BrowserAuditError(
                f"Known vulnerable version unexpectedly passes: {rejected}"
            )


def audit(
    policy_path: Path,
    *,
    dockerfile: Path | None,
    manifest: Path | None,
    as_of: date,
) -> None:
    policy = load_policy(policy_path)
    audit_review(policy, as_of=as_of)
    audit_rejected_versions(policy)
    if dockerfile is not None:
        audit_dockerfile(policy, dockerfile)
    if manifest is not None:
        audit_manifest(policy, manifest)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, default=Path("security/browser-policy.json"))
    parser.add_argument("--dockerfile", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    args = parser.parse_args()
    try:
        audit(
            args.policy,
            dockerfile=args.dockerfile,
            manifest=args.manifest,
            as_of=args.as_of,
        )
    except (BrowserAuditError, KeyError, ValueError, OSError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(
        f"Browser security policy passed: reviewed={load_policy(args.policy)['reviewed_on']} "
        f"expires={load_policy(args.policy)['expires_on']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
