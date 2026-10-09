from __future__ import annotations

import ast
import importlib.metadata
import re
import sys
from pathlib import Path

import scrapeyard


def _section(document: str, name: str) -> str:
    match = re.search(rf"(?ms)^\[{re.escape(name)}\]\s*(.*?)(?=^\[|\Z)", document)
    assert match is not None
    return match.group(1)


def _normalize(distribution: str) -> str:
    return re.sub(r"[-_.]+", "-", distribution).lower()


def test_static_package_metadata_uses_pep621_single_version_source() -> None:
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    project = _section(pyproject, "project")
    poetry = _section(pyproject, "tool.poetry")

    assert 'name = "scrapeyard"' in project
    assert 'version = "0.6.0"' in project
    assert 'requires-python = ">=3.10,<4.0"' in project
    for field in ("name", "version", "description", "authors", "readme"):
        assert re.search(rf"(?m)^{field}\s*=", poetry) is None

    package_init = Path("src/scrapeyard/__init__.py").read_text(encoding="utf-8")
    assert 'version("scrapeyard")' in package_init
    assert "0.6.0" not in package_init
    assert scrapeyard.__version__ == importlib.metadata.version("scrapeyard") == "0.6.0"


def test_every_third_party_import_is_a_declared_dependency() -> None:
    project = _section(Path("pyproject.toml").read_text(encoding="utf-8"), "project")
    dependencies = re.search(r"(?ms)^dependencies = \[(.*?)^\]", project)
    assert dependencies is not None
    declared = {
        _normalize(name)
        for name in re.findall(r'(?m)^\s*"([A-Za-z0-9._-]+)', dependencies.group(1))
    }

    imported: set[str] = set()
    for path in Path("src/scrapeyard").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported.update(alias.name.partition(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.partition(".")[0])
    distributions = importlib.metadata.packages_distributions()

    undeclared = {
        module
        for module in imported - set(sys.stdlib_module_names) - {"scrapeyard"}
        if not declared & {_normalize(name) for name in distributions.get(module, [module])}
    }
    assert not undeclared
