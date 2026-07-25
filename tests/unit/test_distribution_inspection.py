from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts.inspect_distribution import DistributionInspectionError, inspect_distributions


def _write_archives(dist_dir: Path, migration_names: list[str]) -> None:
    dist_dir.mkdir()
    with zipfile.ZipFile(dist_dir / "scrapeyard-0.0.0-py3-none-any.whl", "w") as archive:
        archive.writestr("scrapeyard/__init__.py", "")
        archive.writestr(
            "scrapeyard-0.0.0.dist-info/METADATA",
            "Metadata-Version: 2.4\nName: scrapeyard\nVersion: 0.0.0\n",
        )
        for name in migration_names:
            archive.writestr(f"sql/{name}", "SELECT 1;")

    with tarfile.open(dist_dir / "scrapeyard-0.0.0.tar.gz", "w:gz") as archive:
        members = {
            "scrapeyard-0.0.0/scrapeyard/__init__.py": b"",
            "scrapeyard-0.0.0/PKG-INFO": (
                b"Metadata-Version: 2.4\nName: scrapeyard\nVersion: 0.0.0\n"
            ),
            **{
                f"scrapeyard-0.0.0/sql/{name}": b"SELECT 1;"
                for name in migration_names
            },
        }
        for path, data in members.items():
            info = tarfile.TarInfo(path)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


def _write_project(path: Path, version: str = "0.0.0") -> None:
    path.write_text(f'[project]\nname = "scrapeyard"\nversion = "{version}"\n', encoding="utf-8")


def _write_changelog(path: Path, unreleased: str) -> None:
    path.write_text(
        "# Changelog\n\n"
        f"## Unreleased\n\n{unreleased}"
        "## 0.0.0 — 2026-07-25\n\nReleased.\n",
        encoding="utf-8",
    )


def test_distribution_inspection_accepts_exact_migration_sets(tmp_path: Path) -> None:
    migrations = tmp_path / "sql"
    migrations.mkdir()
    for name in ("001_first.sql", "002_second.sql"):
        (migrations / name).write_text("SELECT 1;", encoding="utf-8")
    dist = tmp_path / "dist"
    _write_archives(dist, ["001_first.sql", "002_second.sql"])
    project = tmp_path / "pyproject.toml"
    _write_project(project)

    inspect_distributions(dist, migrations, project)


def test_distribution_inspection_rejects_missing_migration(tmp_path: Path) -> None:
    migrations = tmp_path / "sql"
    migrations.mkdir()
    for name in ("001_first.sql", "002_second.sql"):
        (migrations / name).write_text("SELECT 1;", encoding="utf-8")
    dist = tmp_path / "dist"
    _write_archives(dist, ["001_first.sql"])
    project = tmp_path / "pyproject.toml"
    _write_project(project)

    with pytest.raises(DistributionInspectionError, match="002_second.sql"):
        inspect_distributions(dist, migrations, project)


def test_distribution_inspection_rejects_version_drift(tmp_path: Path) -> None:
    migrations = tmp_path / "sql"
    migrations.mkdir()
    (migrations / "001_first.sql").write_text("SELECT 1;", encoding="utf-8")
    dist = tmp_path / "dist"
    _write_archives(dist, ["001_first.sql"])
    project = tmp_path / "pyproject.toml"
    _write_project(project, "0.0.1")

    with pytest.raises(DistributionInspectionError, match="does not match project version"):
        inspect_distributions(dist, migrations, project)


def test_normal_distribution_inspection_allows_post_release_changelog(
    tmp_path: Path,
) -> None:
    migrations = tmp_path / "sql"
    migrations.mkdir()
    (migrations / "001_first.sql").write_text("SELECT 1;", encoding="utf-8")
    dist = tmp_path / "dist"
    _write_archives(dist, ["001_first.sql"])
    project = tmp_path / "pyproject.toml"
    _write_project(project)
    changelog = tmp_path / "CHANGELOG.md"
    _write_changelog(changelog, "### Fixed\n- A legitimate follow-up fix.\n\n")

    inspect_distributions(
        dist,
        migrations,
        project,
        changelog,
    )


def test_release_distribution_inspection_requires_empty_unreleased(
    tmp_path: Path,
) -> None:
    migrations = tmp_path / "sql"
    migrations.mkdir()
    (migrations / "001_first.sql").write_text("SELECT 1;", encoding="utf-8")
    dist = tmp_path / "dist"
    _write_archives(dist, ["001_first.sql"])
    project = tmp_path / "pyproject.toml"
    _write_project(project)
    changelog = tmp_path / "CHANGELOG.md"
    _write_changelog(changelog, "### Fixed\n- Must be released first.\n\n")

    with pytest.raises(DistributionInspectionError, match="empty Unreleased"):
        inspect_distributions(
            dist,
            migrations,
            project,
            changelog,
            require_empty_unreleased=True,
        )
