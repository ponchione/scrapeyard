from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_common_package_exports_settings_lazily() -> None:
    from scrapeyard.common import ServiceSettings, get_settings

    assert ServiceSettings.__name__ == "ServiceSettings"
    assert callable(get_settings)


def test_url_guard_can_be_imported_first_in_fresh_interpreter() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from scrapeyard.engine.url_guard import assert_public_url; "
            "assert_public_url('https://example.com', resolve_dns=False)",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_poetry_package_includes_sql_migrations() -> None:
    pyproject = Path("pyproject.toml").read_text()

    assert 'include = [{path = "sql/*.sql", format = ["sdist", "wheel"]}]' in pyproject
