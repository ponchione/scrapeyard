"""Execute installed browser binaries and write their exact runtime inventory."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any


def _binding(component: str, distribution: str, module: Any) -> dict[str, str]:
    browsers = Path(module.__file__).parent / "driver" / "package" / "browsers.json"
    payload = json.loads(browsers.read_text(encoding="utf-8"))
    chromium = next(item for item in payload["browsers"] if item["name"] == "chromium")
    return {
        "component": component,
        "distribution": distribution,
        "version": importlib.metadata.version(distribution),
        "browser_version": str(chromium["browserVersion"]),
        "revision": str(chromium["revision"]),
    }


def _execute_version(path: Path) -> str:
    completed = subprocess.run(
        [str(path), "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
        env={**os.environ, "HOME": os.environ.get("HOME", "/tmp")},
    )
    output = (completed.stdout or completed.stderr).strip()
    if not output:
        raise RuntimeError(f"Browser produced no version output: {path}")
    return output


def _chromium_executable(root: Path, revision: str) -> Path:
    candidates = [
        path
        for path in (root / f"chromium-{revision}").rglob("chrome")
        if path.is_file() and os.access(path, os.X_OK)
    ]
    if not candidates:
        raise FileNotFoundError(f"Chromium revision {revision} executable is missing")
    return sorted(candidates, key=lambda path: (len(path.parts), str(path)))[0]


def inspect_runtime() -> dict[str, Any]:
    import patchright
    import playwright
    from camoufox.multiversion import get_active_path
    from camoufox.pkgman import Version, launch_path

    playwright_binding = _binding("playwright", "playwright", playwright)
    patchright_binding = _binding("patchright", "patchright", patchright)
    if (
        playwright_binding["browser_version"] != patchright_binding["browser_version"]
        or playwright_binding["revision"] != patchright_binding["revision"]
    ):
        raise RuntimeError("Playwright and Patchright do not share the reviewed Chromium build")

    chromium_path = _chromium_executable(
        Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"]),
        playwright_binding["revision"],
    )
    chromium_output = _execute_version(chromium_path)
    if playwright_binding["browser_version"] not in chromium_output:
        raise RuntimeError(
            f"Chromium executable reported {chromium_output!r}, expected "
            f"{playwright_binding['browser_version']!r}"
        )

    active_camoufox = get_active_path()
    if active_camoufox is None:
        raise RuntimeError("Camoufox has no active installed browser")
    camoufox_version = Version.from_path(active_camoufox)
    camoufox_path = Path(launch_path(active_camoufox))
    camoufox_output = _execute_version(camoufox_path)
    reported_numbers = re.findall(r"\d+(?:\.\d+)+", camoufox_output)
    if not reported_numbers or not reported_numbers[0].startswith(
        str(camoufox_version.version)
    ):
        raise RuntimeError(
            f"Camoufox executable reported {camoufox_output!r}, expected "
            f"{camoufox_version.version!r}"
        )

    return {
        "schema_version": 1,
        "components": {
            "scrapling": {
                "version": importlib.metadata.version("scrapling"),
                "distribution": "scrapling",
            },
            "playwright": playwright_binding,
            "patchright": patchright_binding,
            "chromium": {
                "version": playwright_binding["browser_version"],
                "revision": playwright_binding["revision"],
            },
            "camoufox-package": {
                "version": importlib.metadata.version("cloverlabs-camoufox"),
                "distribution": "cloverlabs-camoufox",
            },
            "camoufox-browser": {
                "version": str(camoufox_version.version),
                "release": camoufox_version.build,
            },
        },
        "browser_binaries": [
            {
                "name": "chromium",
                "version": playwright_binding["browser_version"],
                "revision": playwright_binding["revision"],
                "path": str(chromium_path),
                "reported_version": chromium_output,
                "consumers": ["playwright", "patchright"],
            },
            {
                "name": "camoufox",
                "version": str(camoufox_version.version),
                "release": camoufox_version.build,
                "path": str(camoufox_path),
                "reported_version": camoufox_output,
                "consumers": ["cloverlabs-camoufox"],
            },
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = json.dumps(inspect_runtime(), indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(payload, end="")
    else:
        args.output.write_text(payload, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
