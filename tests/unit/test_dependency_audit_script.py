from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from scripts.audit_dependencies import (
    ExceptionConfigError,
    approved_vulnerability_ids,
    audit_development,
    audit_production,
)


def _write_exceptions(path: Path, exceptions: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps({"exceptions": exceptions}), encoding="utf-8")


def _exception(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": "CVE-2099-0001",
        "package": "example-package",
        "scopes": ["production"],
        "owner": "security@example.invalid",
        "rationale": "No compatible fix is available yet.",
        "expires": "2099-02-01",
        "upstream": "https://example.invalid/issues/1",
    }
    entry.update(overrides)
    return entry


def test_approved_vulnerability_ids_are_scoped(tmp_path: Path) -> None:
    exceptions = tmp_path / "exceptions.json"
    _write_exceptions(exceptions, [_exception()])

    assert approved_vulnerability_ids(exceptions, "production", today=date(2099, 1, 1)) == [
        "CVE-2099-0001"
    ]
    assert approved_vulnerability_ids(exceptions, "development", today=date(2099, 1, 1)) == []


def test_expired_exception_fails_closed(tmp_path: Path) -> None:
    exceptions = tmp_path / "exceptions.json"
    _write_exceptions(exceptions, [_exception(expires="2026-07-11")])

    with pytest.raises(ExceptionConfigError, match="expired on 2026-07-11"):
        approved_vulnerability_ids(exceptions, "production", today=date(2026, 7, 12))


@pytest.mark.parametrize("field", ["id", "package", "owner", "rationale", "upstream"])
def test_exception_requires_review_metadata(tmp_path: Path, field: str) -> None:
    exceptions = tmp_path / "exceptions.json"
    entry = _exception()
    del entry[field]
    _write_exceptions(exceptions, [entry])

    with pytest.raises(ExceptionConfigError, match="missing"):
        approved_vulnerability_ids(exceptions, "production")


def test_development_audit_skips_only_the_editable_project(tmp_path: Path) -> None:
    exceptions = tmp_path / "exceptions.json"
    _write_exceptions(exceptions, [])
    commands: list[list[str]] = []

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        commands.append(command)
        assert kwargs["check"] is True
        return subprocess.CompletedProcess(command, 0)

    audit_development(exceptions, runner=runner)

    assert commands == [
        [
            sys.executable,
            "-m",
            "pip_audit",
            "--local",
            "--skip-editable",
            "--progress-spinner=off",
        ]
    ]


def test_production_audit_exports_locked_main_dependencies(tmp_path: Path) -> None:
    exceptions = tmp_path / "exceptions.json"
    _write_exceptions(exceptions, [_exception()])
    commands: list[list[str]] = []

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        commands.append(command)
        assert kwargs["check"] is True
        return subprocess.CompletedProcess(command, 0)

    audit_production(exceptions, runner=runner)

    assert commands[0][:5] == [
        "poetry",
        "export",
        "--only",
        "main",
        "--format=requirements.txt",
    ]
    assert "--without-hashes" not in commands[0]
    assert commands[1][:4] == [sys.executable, "-m", "pip_audit", "--requirement"]
    assert "--no-deps" in commands[1]
    assert "--disable-pip" in commands[1]
    assert commands[1][-2:] == ["--ignore-vuln", "CVE-2099-0001"]


def test_docker_and_ci_pin_audited_packaging_tools() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    workflow = Path(".github/workflows/dependency-audit.yml").read_text(encoding="utf-8")

    assert "ARG PIP_VERSION=26.1.2" in dockerfile
    assert '"pip==${PIP_VERSION}"' in dockerfile
    assert "ARG POETRY_VERSION=2.3.4" in dockerfile
    assert '"poetry==${POETRY_VERSION}"' in dockerfile
    assert "ARG POETRY_EXPORT_VERSION=1.10.0" in dockerfile
    assert '"poetry-plugin-export==${POETRY_EXPORT_VERSION}"' in dockerfile

    assert "pip==26.1.2" in workflow
    assert "poetry==2.3.4" in workflow
    assert "poetry-plugin-export==1.10.0" in workflow
    assert "python -m pip_audit --local --progress-spinner=off" in workflow
    assert "poetry run python scripts/audit_dependencies.py all" in workflow
