from __future__ import annotations

import importlib.metadata
import re
from pathlib import Path

import scrapeyard


def _section(document: str, name: str) -> str:
    match = re.search(rf"(?ms)^\[{re.escape(name)}\]\s*(.*?)(?=^\[|\Z)", document)
    assert match is not None
    return match.group(1)


def test_static_package_metadata_uses_pep621_single_version_source() -> None:
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    project = _section(pyproject, "project")
    poetry = _section(pyproject, "tool.poetry")

    assert 'name = "scrapeyard"' in project
    assert 'version = "0.7.0"' in project
    assert 'requires-python = ">=3.10,<4.0"' in project
    for field in ("name", "version", "description", "authors", "readme"):
        assert re.search(rf"(?m)^{field}\s*=", poetry) is None

    package_init = Path("src/scrapeyard/__init__.py").read_text(encoding="utf-8")
    assert 'version("scrapeyard")' in package_init
    assert "0.7.0" not in package_init
    assert scrapeyard.__version__ == importlib.metadata.version("scrapeyard") == "0.7.0"
