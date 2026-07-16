"""Verify that built distributions contain every checked-in SQL migration."""

from __future__ import annotations

import argparse
import re
import tarfile
import zipfile
from email.parser import Parser
from pathlib import Path, PurePosixPath


class DistributionInspectionError(RuntimeError):
    """Raised when distribution contents do not match the source migrations."""


def _migration_names(members: list[str]) -> set[str]:
    return {
        path.name
        for member in members
        if (path := PurePosixPath(member)).parent.name == "sql" and path.suffix == ".sql"
    }


def _project_version(project_file: Path) -> str:
    text = project_file.read_text(encoding="utf-8")
    project = re.search(r"(?ms)^\[project\]\s*(.*?)(?=^\[|\Z)", text)
    if project is None:
        raise DistributionInspectionError(f"Missing [project] metadata in {project_file}")
    version = re.search(r'^version\s*=\s*"([^"]+)"\s*$', project.group(1), re.MULTILINE)
    if version is None:
        raise DistributionInspectionError(f"Missing static project version in {project_file}")
    return version.group(1)


def _metadata_version(document: str, archive: Path) -> str:
    metadata = Parser().parsestr(document)
    if metadata.get("Name") != "scrapeyard":
        raise DistributionInspectionError(f"{archive} has unexpected package name")
    version = metadata.get("Version")
    if not version:
        raise DistributionInspectionError(f"{archive} has no package version metadata")
    return version


def _contains_package(members: list[str]) -> bool:
    return any(PurePosixPath(member).parts[-2:] == ("scrapeyard", "__init__.py") for member in members)


def inspect_distributions(
    dist_dir: Path,
    migrations_dir: Path = Path("sql"),
    project_file: Path = Path("pyproject.toml"),
    changelog_file: Path | None = None,
) -> None:
    """Require complete package, version metadata, and SQL in wheel and sdist."""
    expected = {path.name for path in migrations_dir.glob("*.sql")}
    if not expected:
        raise DistributionInspectionError(f"No SQL migrations found in {migrations_dir}")

    wheels = sorted(dist_dir.glob("*.whl"))
    sdists = sorted(dist_dir.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise DistributionInspectionError(
            f"Expected one wheel and one sdist in {dist_dir}; "
            f"found {len(wheels)} wheel(s) and {len(sdists)} sdist(s)"
        )

    expected_version = _project_version(project_file)
    if changelog_file is not None:
        changelog = changelog_file.read_text(encoding="utf-8")
        released = re.search(
            rf"(?m)^## {re.escape(expected_version)} — (\d{{4}}-\d{{2}}-\d{{2}})$",
            changelog,
        )
        if released is None:
            raise DistributionInspectionError(
                f"{changelog_file} has no dated release for {expected_version}"
            )
        unreleased = re.search(
            r"(?ms)^## Unreleased\s*(.*?)^## ",
            changelog,
        )
        if unreleased is None or unreleased.group(1).strip():
            raise DistributionInspectionError(
                f"{changelog_file} must retain an empty Unreleased section"
            )
    with zipfile.ZipFile(wheels[0]) as archive:
        wheel_members = archive.namelist()
        wheel_migrations = _migration_names(wheel_members)
        metadata_members = [name for name in wheel_members if name.endswith(".dist-info/METADATA")]
        if len(metadata_members) != 1:
            raise DistributionInspectionError(f"{wheels[0]} must contain one METADATA file")
        wheel_version = _metadata_version(
            archive.read(metadata_members[0]).decode("utf-8"),
            wheels[0],
        )
    with tarfile.open(sdists[0], mode="r:gz") as archive:
        sdist_members = archive.getnames()
        sdist_migrations = _migration_names(sdist_members)
        metadata_members = [name for name in sdist_members if name.endswith("/PKG-INFO")]
        if len(metadata_members) != 1:
            raise DistributionInspectionError(f"{sdists[0]} must contain one PKG-INFO file")
        metadata_file = archive.extractfile(metadata_members[0])
        if metadata_file is None:
            raise DistributionInspectionError(f"Unable to read metadata from {sdists[0]}")
        sdist_version = _metadata_version(metadata_file.read().decode("utf-8"), sdists[0])

    for archive, members in ((wheels[0], wheel_members), (sdists[0], sdist_members)):
        if not _contains_package(members):
            raise DistributionInspectionError(f"{archive} does not contain the scrapeyard package")

    for archive, version in ((wheels[0], wheel_version), (sdists[0], sdist_version)):
        if version != expected_version:
            raise DistributionInspectionError(
                f"{archive} version {version!r} does not match project version {expected_version!r}"
            )

    for archive, actual in ((wheels[0], wheel_migrations), (sdists[0], sdist_migrations)):
        if actual != expected:
            missing = sorted(expected - actual)
            unexpected = sorted(actual - expected)
            raise DistributionInspectionError(
                f"{archive} SQL migrations differ from source; "
                f"missing={missing}, unexpected={unexpected}"
            )
        print(
            f"{archive}: package version {expected_version}; "
            f"contains all {len(expected)} SQL migrations"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist_dir", nargs="?", type=Path, default=Path("dist"))
    args = parser.parse_args()
    inspect_distributions(args.dist_dir, changelog_file=Path("CHANGELOG.md"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
