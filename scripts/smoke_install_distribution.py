#!/usr/bin/env python3
"""Install the built wheel without dependencies and verify package metadata."""

from __future__ import annotations

import argparse
import subprocess
import tempfile
import venv
from pathlib import Path

if __package__:
    from .inspect_distribution import DistributionInspectionError, _project_version
else:
    from inspect_distribution import DistributionInspectionError, _project_version


def smoke_install(dist_dir: Path, project_file: Path = Path("pyproject.toml")) -> None:
    wheels = sorted(dist_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise DistributionInspectionError(
            f"Expected one wheel in {dist_dir}; found {len(wheels)}"
        )
    expected_version = _project_version(project_file)

    with tempfile.TemporaryDirectory(prefix="scrapeyard-wheel-smoke-") as temp_dir:
        environment = Path(temp_dir) / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(environment)
        python = environment / ("Scripts/python.exe" if environment.name == "Scripts" else "bin/python")
        if not python.exists():
            python = environment / "Scripts" / "python.exe"
        subprocess.run(
            [str(python), "-m", "pip", "install", "--no-deps", str(wheels[0].resolve())],
            check=True,
            cwd=temp_dir,
        )
        verification = (
            "import importlib.metadata as metadata; import scrapeyard; "
            f"expected={expected_version!r}; "
            "installed=metadata.version('scrapeyard'); "
            "assert installed == expected, (installed, expected); "
            "assert scrapeyard.__version__ == expected, "
            "(scrapeyard.__version__, expected); "
            "print(f'scrapeyard {installed} wheel installation verified')"
        )
        subprocess.run([str(python), "-c", verification], check=True, cwd=temp_dir)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist_dir", nargs="?", type=Path, default=Path("dist"))
    parser.add_argument("--project-file", type=Path, default=Path("pyproject.toml"))
    args = parser.parse_args()
    smoke_install(args.dist_dir, args.project_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
