"""Fail on unaccepted Medium, High, or Critical Trivy findings."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any


BLOCKING_SEVERITIES = {"MEDIUM", "HIGH", "CRITICAL"}


def _finding_records(report: dict[str, Any]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for result in report.get("Results") or []:
        target = str(result.get("Target", ""))
        target_scope = target
        if result.get("Class") == "os-pkgs" and target.endswith(")") and " (" in target:
            target_scope = target.rsplit(" (", 1)[1][:-1]
        for finding in result.get("Vulnerabilities") or []:
            findings.append(
                {
                    "kind": "vulnerability",
                    "id": str(finding.get("VulnerabilityID", "")),
                    "package": str(finding.get("PkgName", "")),
                    "installed_version": str(finding.get("InstalledVersion", "")),
                    "target": target,
                    "target_scope": target_scope,
                    "severity": str(finding.get("Severity", "")).upper(),
                }
            )
        for finding in result.get("Misconfigurations") or []:
            findings.append(
                {
                    "kind": "misconfiguration",
                    "id": str(finding.get("ID") or finding.get("AVDID") or ""),
                    "package": "",
                    "installed_version": "",
                    "target": target,
                    "target_scope": target_scope,
                    "severity": str(finding.get("Severity", "")).upper(),
                }
            )
        for finding in result.get("Secrets") or []:
            findings.append(
                {
                    "kind": "secret",
                    "id": str(finding.get("RuleID", "")),
                    "package": "",
                    "installed_version": "",
                    "target": target,
                    "target_scope": target_scope,
                    "severity": str(finding.get("Severity", "")).upper(),
                }
            )
    return [item for item in findings if item["severity"] in BLOCKING_SEVERITIES]


def _validate_exception(entry: dict[str, Any], *, today: date, maximum_days: int) -> None:
    if str(entry.get("severity", "")).upper() != "MEDIUM":
        raise ValueError(
            f"only Medium container findings may be accepted: {entry.get('id', '')}"
        )
    for field in ("kind", "id"):
        if not str(entry.get(field, "")).strip():
            raise ValueError(
                f"container finding exception lacks exact {field}: {entry.get('id', '')}"
            )
    if entry["kind"] == "vulnerability":
        affected_packages = entry.get("affected_packages")
        if affected_packages is not None:
            if (
                not isinstance(affected_packages, list)
                or not affected_packages
                or not all(
                    isinstance(item, dict)
                    and str(item.get("package", "")).strip()
                    and str(item.get("installed_version", "")).strip()
                    for item in affected_packages
                )
            ):
                raise ValueError(
                    f"container vulnerability exception lacks exact affected packages: "
                    f"{entry['id']}"
                )
            pairs = {
                (str(item["package"]), str(item["installed_version"]))
                for item in affected_packages
            }
            if len(pairs) != len(affected_packages):
                raise ValueError(
                    f"container vulnerability exception repeats an affected package: "
                    f"{entry['id']}"
                )
        else:
            for field in ("package", "installed_version"):
                if not str(entry.get(field, "")).strip():
                    raise ValueError(
                        f"container vulnerability exception lacks exact {field}: "
                        f"{entry['id']}"
                    )
    if not str(entry.get("target", "")).strip() and not str(
        entry.get("target_scope", "")
    ).strip():
        raise ValueError(
            f"container finding exception lacks target or target_scope: {entry['id']}"
        )
    accepted = date.fromisoformat(str(entry["accepted_on"]))
    expires = date.fromisoformat(str(entry["expires_on"]))
    if expires < today:
        raise ValueError(f"expired container finding exception: {entry['id']}")
    if expires < accepted or (expires - accepted).days > maximum_days:
        raise ValueError(f"container finding exception is too broad: {entry['id']}")
    for field in ("owner", "rationale"):
        if not str(entry.get(field, "")).strip():
            raise ValueError(f"container finding exception lacks {field}: {entry['id']}")
    controls = entry.get("compensating_controls")
    if not isinstance(controls, list) or not controls or not all(
        isinstance(control, str) and control.strip() for control in controls
    ):
        raise ValueError(
            f"container finding exception lacks compensating controls: {entry['id']}"
        )


def _matches(finding: dict[str, str], entry: dict[str, Any]) -> bool:
    affected_packages = entry.get("affected_packages")
    if affected_packages is not None and (
        finding["package"], finding["installed_version"]
    ) not in {
        (str(item["package"]), str(item["installed_version"]))
        for item in affected_packages
    }:
        return False
    for field in (
        "kind",
        "id",
        "target",
        "target_scope",
        "severity",
    ):
        expected = entry.get(field)
        if expected is not None and str(expected) != finding[field]:
            return False
    if affected_packages is None:
        for field in ("package", "installed_version"):
            expected = entry.get(field)
            if expected is not None and str(expected) != finding[field]:
                return False
    return True


def evaluate(
    reports: list[dict[str, Any]],
    policy: dict[str, Any],
    *,
    today: date,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    if policy.get("schema_version") != 1:
        raise ValueError("unsupported container scan exception schema")
    maximum_days = int(policy["maximum_acceptance_days"])
    exceptions = policy.get("exceptions")
    if not isinstance(exceptions, list):
        raise ValueError("container scan exceptions must be a list")
    for entry in exceptions:
        if not isinstance(entry, dict):
            raise ValueError("container scan exception must be an object")
        _validate_exception(entry, today=today, maximum_days=maximum_days)

    findings = [finding for report in reports for finding in _finding_records(report)]
    accepted: list[dict[str, str]] = []
    unaccepted: list[dict[str, str]] = []
    for finding in findings:
        if any(_matches(finding, entry) for entry in exceptions):
            accepted.append(finding)
        else:
            unaccepted.append(finding)
    return accepted, unaccepted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument(
        "--exceptions",
        type=Path,
        default=Path("security/container-scan-exceptions.json"),
    )
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    args = parser.parse_args()
    try:
        accepted, unaccepted = evaluate(
            [json.loads(path.read_text(encoding="utf-8")) for path in args.reports],
            json.loads(args.exceptions.read_text(encoding="utf-8")),
            today=args.as_of,
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(f"Accepted bounded findings: {len(accepted)}")
    if unaccepted:
        for finding in unaccepted:
            print(
                f"UNACCEPTED {finding['severity']} {finding['kind']} {finding['id']} "
                f"package={finding['package']} version={finding['installed_version']} "
                f"target={finding['target']}"
            )
        return 1
    print("No unaccepted Medium, High, or Critical container findings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
