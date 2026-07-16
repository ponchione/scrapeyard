"""Add executed browser binaries and their real versions to a CycloneDX SBOM."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def augment(sbom: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    if sbom.get("bomFormat") != "CycloneDX":
        raise ValueError("SBOM is not CycloneDX")
    binaries = runtime.get("browser_binaries")
    if not isinstance(binaries, list) or len(binaries) < 2:
        raise ValueError("browser runtime manifest is incomplete")

    components = sbom.setdefault("components", [])
    if not isinstance(components, list):
        raise ValueError("CycloneDX components must be a list")
    existing_refs = {
        str(component.get("bom-ref"))
        for component in components
        if isinstance(component, dict)
    }
    for binary in binaries:
        name = str(binary["name"])
        version = str(binary["version"])
        bom_ref = f"pkg:generic/scrapeyard-{name}-binary@{version}"
        if bom_ref in existing_refs:
            continue
        properties = [
            {"name": "scrapeyard:runtime:path", "value": str(binary["path"])},
            {
                "name": "scrapeyard:runtime:reported-version",
                "value": str(binary["reported_version"]),
            },
            {
                "name": "scrapeyard:runtime:consumers",
                "value": ",".join(str(value) for value in binary.get("consumers", [])),
            },
        ]
        for field in ("revision", "release"):
            if binary.get(field):
                properties.append(
                    {
                        "name": f"scrapeyard:runtime:{field}",
                        "value": str(binary[field]),
                    }
                )
        components.append(
            {
                "type": "application",
                "bom-ref": bom_ref,
                "name": f"scrapeyard-{name}-browser-binary",
                "version": version,
                "purl": bom_ref,
                "properties": properties,
            }
        )
        existing_refs.add(bom_ref)
    return sbom


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sbom", type=Path)
    parser.add_argument("runtime_manifest", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = augment(
        json.loads(args.sbom.read_text(encoding="utf-8")),
        json.loads(args.runtime_manifest.read_text(encoding="utf-8")),
    )
    output = args.output or args.sbom
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
