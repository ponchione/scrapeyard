#!/usr/bin/env python3
"""Audit Scrapeyard's development and production dependency sets."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXCEPTIONS = ROOT / "security" / "dependency-audit-exceptions.json"
SCOPES = frozenset({"development", "production"})
REQUIRED_EXCEPTION_FIELDS = frozenset(
    {"id", "package", "scopes", "owner", "rationale", "expires", "upstream"}
)


class ExceptionConfigError(ValueError):
    """Raised when an audit exception is missing required review metadata."""


def _nonempty_string(entry: dict[str, Any], field: str, index: int) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ExceptionConfigError(f"exception {index}: {field!r} must be a non-empty string")
    return value.strip()


def approved_vulnerability_ids(
    path: Path,
    scope: str,
    *,
    today: date | None = None,
) -> list[str]:
    """Return reviewed, unexpired vulnerability IDs approved for one audit scope."""
    if scope not in SCOPES:
        raise ExceptionConfigError(f"unknown audit scope: {scope}")

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ExceptionConfigError(f"audit exception file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ExceptionConfigError(f"invalid JSON in {path}: {exc}") from exc

    if not isinstance(document, dict) or set(document) != {"exceptions"}:
        raise ExceptionConfigError("exception file must contain only an 'exceptions' array")
    entries = document["exceptions"]
    if not isinstance(entries, list):
        raise ExceptionConfigError("'exceptions' must be an array")

    current_date = today or date.today()
    approved: list[str] = []
    seen: set[str] = set()
    for index, raw_entry in enumerate(entries, start=1):
        if not isinstance(raw_entry, dict):
            raise ExceptionConfigError(f"exception {index}: entry must be an object")
        missing = REQUIRED_EXCEPTION_FIELDS - set(raw_entry)
        extra = set(raw_entry) - REQUIRED_EXCEPTION_FIELDS
        if missing or extra:
            details = []
            if missing:
                details.append(f"missing {sorted(missing)}")
            if extra:
                details.append(f"unexpected {sorted(extra)}")
            raise ExceptionConfigError(f"exception {index}: {', '.join(details)}")

        vulnerability_id = _nonempty_string(raw_entry, "id", index)
        _nonempty_string(raw_entry, "package", index)
        _nonempty_string(raw_entry, "owner", index)
        _nonempty_string(raw_entry, "rationale", index)
        upstream = _nonempty_string(raw_entry, "upstream", index)
        if not upstream.startswith(("https://", "http://")):
            raise ExceptionConfigError(f"exception {index}: 'upstream' must be an HTTP(S) URL")

        raw_scopes = raw_entry["scopes"]
        if (
            not isinstance(raw_scopes, list)
            or not raw_scopes
            or any(not isinstance(item, str) for item in raw_scopes)
        ):
            raise ExceptionConfigError(f"exception {index}: 'scopes' must be a non-empty array")
        entry_scopes = set(raw_scopes)
        if not entry_scopes <= SCOPES:
            raise ExceptionConfigError(
                f"exception {index}: unknown scopes {sorted(entry_scopes - SCOPES)}"
            )

        raw_expiry = _nonempty_string(raw_entry, "expires", index)
        try:
            expiry = date.fromisoformat(raw_expiry)
        except ValueError as exc:
            raise ExceptionConfigError(
                f"exception {index}: 'expires' must use YYYY-MM-DD"
            ) from exc
        if expiry < current_date:
            raise ExceptionConfigError(
                f"exception {index}: {vulnerability_id} expired on {expiry.isoformat()}"
            )
        if vulnerability_id in seen:
            raise ExceptionConfigError(f"exception {index}: duplicate ID {vulnerability_id}")
        seen.add(vulnerability_id)
        if scope in entry_scopes:
            approved.append(vulnerability_id)

    return approved


def _ignore_arguments(vulnerability_ids: Sequence[str]) -> list[str]:
    return [argument for item in vulnerability_ids for argument in ("--ignore-vuln", item)]


def audit_development(
    exceptions_path: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> None:
    """Audit every installed dependency in the Poetry development environment."""
    approved = approved_vulnerability_ids(exceptions_path, "development")
    command = [
        sys.executable,
        "-m",
        "pip_audit",
        "--local",
        "--skip-editable",
        "--progress-spinner=off",
        *_ignore_arguments(approved),
    ]
    runner(command, cwd=ROOT, check=True)


def export_production_requirements(
    output: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> None:
    """Export only the locked main dependency group using Poetry's export plugin."""
    command = [
        "poetry",
        "export",
        "--only",
        "main",
        "--format=requirements.txt",
        f"--output={output}",
    ]
    try:
        runner(command, cwd=ROOT, check=True)
    except FileNotFoundError as exc:
        raise RuntimeError("Poetry is required to export production dependencies") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            "production export failed; Poetry 2.x requires poetry-plugin-export"
        ) from exc


def audit_production(
    exceptions_path: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> None:
    """Export and audit the exact production dependency graph."""
    approved = approved_vulnerability_ids(exceptions_path, "production")
    with tempfile.TemporaryDirectory(prefix="scrapeyard-dependency-audit-") as temp_dir:
        requirements = Path(temp_dir) / "requirements.txt"
        export_production_requirements(requirements, runner=runner)
        command = [
            sys.executable,
            "-m",
            "pip_audit",
            "--requirement",
            str(requirements),
            "--no-deps",
            "--disable-pip",
            "--strict",
            "--progress-spinner=off",
            *_ignore_arguments(approved),
        ]
        runner(command, cwd=ROOT, check=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "scope",
        choices=("development", "production", "all", "export-production"),
        help="dependency set to audit, or export the production set without auditing",
    )
    parser.add_argument(
        "--exceptions",
        type=Path,
        default=DEFAULT_EXCEPTIONS,
        help=f"reviewed exception file (default: {DEFAULT_EXCEPTIONS.relative_to(ROOT)})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="requirements output path (required with export-production)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.scope == "export-production":
        if args.output is None:
            _parser().error("--output is required with export-production")
        export_production_requirements(args.output.resolve())
        return 0
    if args.output is not None:
        _parser().error("--output is only valid with export-production")

    try:
        if args.scope in {"development", "all"}:
            audit_development(args.exceptions.resolve())
        if args.scope in {"production", "all"}:
            audit_production(args.exceptions.resolve())
    except (ExceptionConfigError, RuntimeError) as exc:
        print(f"dependency audit configuration error: {exc}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        return exc.returncode or 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
