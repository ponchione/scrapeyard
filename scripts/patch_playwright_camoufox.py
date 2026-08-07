"""Patch Playwright's Firefox adapter for Camoufox page errors without locations."""

from __future__ import annotations

import argparse
import importlib.metadata
from pathlib import Path


SUPPORTED_PLAYWRIGHT_VERSION = "1.61.0"
VULNERABLE_HANDLER = "this._page.addPageError(error, params2.location);"
PATCHED_HANDLER = (
    'this._page.addPageError(error, params2.location ?? '
    '{ url: "", lineNumber: 0, columnNumber: 0 });'
)


def playwright_bundle() -> Path:
    import playwright

    return (
        Path(playwright.__file__).parent
        / "driver"
        / "package"
        / "lib"
        / "coreBundle.js"
    )


def verify_bundle(bundle: Path) -> None:
    source = bundle.read_text(encoding="utf-8")
    vulnerable_count = source.count(VULNERABLE_HANDLER)
    patched_count = source.count(PATCHED_HANDLER)
    if vulnerable_count or patched_count != 1:
        raise RuntimeError(
            "Playwright Camoufox compatibility patch verification failed: "
            f"vulnerable={vulnerable_count}, patched={patched_count}"
        )


def patch_bundle(bundle: Path) -> bool:
    source = bundle.read_text(encoding="utf-8")
    vulnerable_count = source.count(VULNERABLE_HANDLER)
    patched_count = source.count(PATCHED_HANDLER)
    if vulnerable_count == 0 and patched_count == 1:
        return False
    if vulnerable_count != 1 or patched_count != 0:
        raise RuntimeError(
            "Unsupported Playwright Firefox page-error handler: "
            f"vulnerable={vulnerable_count}, patched={patched_count}"
        )
    bundle.write_text(
        source.replace(VULNERABLE_HANDLER, PATCHED_HANDLER),
        encoding="utf-8",
    )
    verify_bundle(bundle)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    installed_version = importlib.metadata.version("playwright")
    if installed_version != SUPPORTED_PLAYWRIGHT_VERSION:
        parser.error(
            "Playwright Camoufox compatibility patch supports "
            f"{SUPPORTED_PLAYWRIGHT_VERSION}, found {installed_version}"
        )

    bundle = playwright_bundle()
    if args.check:
        verify_bundle(bundle)
    else:
        patch_bundle(bundle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
