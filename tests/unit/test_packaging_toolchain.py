from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts.check_packaging_toolchain import (
    PIP_VERSION,
    POETRY_EXPORT_VERSION,
    POETRY_VERSION,
    ToolchainVersionError,
    check_packaging_toolchain,
)


def _runner_for(outputs: list[str]):
    remaining = iter(outputs)

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert kwargs == {
            "check": True,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
        }
        return subprocess.CompletedProcess(command, 0, stdout=next(remaining))

    return runner


def test_packaging_toolchain_accepts_exact_versions() -> None:
    check_packaging_toolchain(
        runner=_runner_for(
            [
                "pip 26.1.2 from /tmp/site-packages/pip (python 3.12)\n",
                "Poetry (version 2.3.4)\n",
                "  - poetry-plugin-export (1.10.0) Poetry plugin\n",
            ]
        )
    )


def test_packaging_toolchain_rejects_version_drift() -> None:
    with pytest.raises(ToolchainVersionError, match="Poetry"):
        check_packaging_toolchain(
            runner=_runner_for(
                [
                    "pip 26.1.2 from /tmp/site-packages/pip (python 3.12)\n",
                    "Poetry (version 2.3.3)\n",
                    "  - poetry-plugin-export (1.10.0) Poetry plugin\n",
                ]
            )
        )


def test_repository_surfaces_declare_and_check_the_same_toolchain() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    ci = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    audit = Path(".github/workflows/dependency-audit.yml").read_text(encoding="utf-8")
    docs = Path("docs/TESTING.md").read_text(encoding="utf-8")

    assert f"ARG PIP_VERSION={PIP_VERSION}" in dockerfile
    assert f"ARG POETRY_VERSION={POETRY_VERSION}" in dockerfile
    assert f"ARG POETRY_EXPORT_VERSION={POETRY_EXPORT_VERSION}" in dockerfile
    assert f'PIP_VERSION: "{PIP_VERSION}"' in ci
    assert f'POETRY_VERSION: "{POETRY_VERSION}"' in ci
    assert f'POETRY_EXPORT_VERSION: "{POETRY_EXPORT_VERSION}"' in ci
    assert f"pip=={PIP_VERSION}" in audit
    assert f"poetry=={POETRY_VERSION}" in audit
    assert f"poetry-plugin-export=={POETRY_EXPORT_VERSION}" in audit
    assert f"Poetry {POETRY_VERSION}" in docs
    for document in (dockerfile, ci, audit):
        assert "check_packaging_toolchain.py" in document
