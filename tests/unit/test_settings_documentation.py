"""The README settings table documents every ServiceSettings field."""

from __future__ import annotations

import re
from pathlib import Path

from scrapeyard.common.settings import ServiceSettings

_ROW_RE = re.compile(r"^\| `(SCRAPEYARD_[A-Z0-9_]+)` \| ([^|]*) \|", re.MULTILINE)


def _readme_settings() -> dict[str, str]:
    readme = Path("README.md").read_text(encoding="utf-8")
    section = readme.split("\n## Settings\n", 1)[1].split("\n## ", 1)[0]
    rows = _ROW_RE.findall(section)
    names = [name for name, _default in rows]
    assert len(names) == len(set(names)), "duplicate README settings rows"
    return {name: default.strip() for name, default in rows}


def _documented_default(value: object) -> str:
    """Spell a code default the way the README's Default column does."""

    if value == "":
        return "empty"
    if isinstance(value, bool):
        return f"`{str(value).lower()}`"
    if isinstance(value, float) and value.is_integer():
        return f"`{int(value)}`"
    return f"`{value}`"


def test_readme_documents_every_setting_and_nothing_else() -> None:
    documented = set(_readme_settings())
    fields = {f"SCRAPEYARD_{name.upper()}" for name in ServiceSettings.model_fields}

    assert sorted(fields - documented) == []
    assert sorted(documented - fields) == []


def test_readme_defaults_match_the_code() -> None:
    # The Default cell starts with the code default; a deployment that
    # overrides it adds a note after it, such as "(`3072` in production
    # Compose)", which is not compared.
    documented = _readme_settings()
    mismatched = {}
    for field_name, field in ServiceSettings.model_fields.items():
        name = f"SCRAPEYARD_{field_name.upper()}"
        expected = _documented_default(field.default)
        cell = documented[name]
        if cell != expected and not cell.startswith(f"{expected} ("):
            mismatched[name] = (cell, expected)

    assert mismatched == {}
