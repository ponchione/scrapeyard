"""Assert OCI labels, installed package identity, and browser runtime provenance."""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any


REQUIRED_LABELS = (
    "org.opencontainers.image.source",
    "org.opencontainers.image.version",
    "org.opencontainers.image.revision",
    "org.opencontainers.image.created",
    "org.opencontainers.image.documentation",
)


def _output(command: list[str]) -> str:
    return subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout.strip()


def inspect_image(
    image: str,
    *,
    expected_version: str,
    expected_revision: str,
) -> dict[str, Any]:
    inspection = json.loads(_output(["docker", "image", "inspect", image]))[0]
    labels = inspection.get("Config", {}).get("Labels") or {}
    missing = [label for label in REQUIRED_LABELS if not labels.get(label)]
    if missing:
        raise ValueError(f"image lacks required OCI labels: {missing}")
    if labels["org.opencontainers.image.version"] != expected_version:
        raise ValueError("OCI application version does not match the release version")
    if labels["org.opencontainers.image.revision"] != expected_revision:
        raise ValueError("OCI source revision does not match the qualified revision")
    datetime.fromisoformat(labels["org.opencontainers.image.created"].replace("Z", "+00:00"))

    package_version = _output(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "python",
            inspection["Id"],
            "-c",
            "import importlib.metadata as m; print(m.version('scrapeyard'))",
        ]
    )
    if package_version != expected_version:
        raise ValueError("installed package version does not match the OCI version")

    runtime_text = _output(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "python",
            inspection["Id"],
            "-c",
            "from pathlib import Path; print(Path('/usr/share/scrapeyard-browser-runtime.json').read_text())",
        ]
    )
    runtime = json.loads(runtime_text)
    components = runtime["components"]
    label_component_pairs = {
        "org.scrapeyard.browser.scrapling.version": "scrapling",
        "org.scrapeyard.browser.playwright.version": "playwright",
        "org.scrapeyard.browser.patchright.version": "patchright",
        "org.scrapeyard.browser.chromium.version": "chromium",
        "org.scrapeyard.browser.camoufox-package.version": "camoufox-package",
        "org.scrapeyard.browser.camoufox.version": "camoufox-browser",
    }
    for label, component in label_component_pairs.items():
        if labels.get(label) != str(components[component]["version"]):
            raise ValueError(f"OCI browser label differs from installed {component}")

    return {
        "schema_version": 1,
        "image_reference": image,
        "image_id": inspection["Id"],
        "repo_digests": inspection.get("RepoDigests") or [],
        "source_revision": expected_revision,
        "package_version": package_version,
        "created": labels["org.opencontainers.image.created"],
        "source": labels["org.opencontainers.image.source"],
        "documentation": labels["org.opencontainers.image.documentation"],
        "browser_runtime": runtime,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        payload = inspect_image(
            args.image,
            expected_version=args.expected_version,
            expected_revision=args.expected_revision,
        )
    except (ValueError, KeyError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
