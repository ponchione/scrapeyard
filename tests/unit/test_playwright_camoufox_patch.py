from __future__ import annotations

from pathlib import Path

import pytest

from scripts.patch_playwright_camoufox import (
    PATCHED_HANDLER,
    VULNERABLE_HANDLER,
    patch_bundle,
    verify_bundle,
)


def test_patch_normalizes_missing_page_error_location_and_is_idempotent(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "coreBundle.js"
    bundle.write_text(f"before\n{VULNERABLE_HANDLER}\nafter\n", encoding="utf-8")

    assert patch_bundle(bundle) is True
    assert bundle.read_text(encoding="utf-8") == f"before\n{PATCHED_HANDLER}\nafter\n"
    verify_bundle(bundle)
    assert patch_bundle(bundle) is False


@pytest.mark.parametrize(
    "source",
    [
        "handler changed upstream",
        f"{VULNERABLE_HANDLER}\n{VULNERABLE_HANDLER}",
        f"{VULNERABLE_HANDLER}\n{PATCHED_HANDLER}",
    ],
)
def test_patch_rejects_an_unreviewed_driver_shape(tmp_path: Path, source: str) -> None:
    bundle = tmp_path / "coreBundle.js"
    bundle.write_text(source, encoding="utf-8")

    with pytest.raises(RuntimeError, match="Unsupported Playwright"):
        patch_bundle(bundle)
