#!/usr/bin/env python3
"""Verify the exact packaging toolchain required by Scrapeyard builds."""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from typing import Any

PIP_VERSION = "26.1.2"
POETRY_VERSION = "2.3.4"
POETRY_EXPORT_VERSION = "1.10.0"


class ToolchainVersionError(RuntimeError):
    """Raised when an installed packaging tool does not match the release pin."""


def _output(
    command: Sequence[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> str:
    try:
        result = runner(
            list(command),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise ToolchainVersionError(f"unable to run {' '.join(command)}") from exc
    return str(result.stdout)


def check_packaging_toolchain(
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> None:
    """Require the pinned pip, Poetry, and export-plugin versions."""

    pip_output = _output([sys.executable, "-m", "pip", "--version"], runner=runner)
    poetry_output = _output(["poetry", "--version"], runner=runner)
    plugins_output = _output(["poetry", "self", "show", "plugins"], runner=runner)

    expected = {
        "pip": (rf"\bpip {re.escape(PIP_VERSION)}\b", pip_output),
        "Poetry": (rf"\bversion {re.escape(POETRY_VERSION)}\b", poetry_output),
        "poetry-plugin-export": (
            rf"\bpoetry-plugin-export \({re.escape(POETRY_EXPORT_VERSION)}\)",
            plugins_output,
        ),
    }
    mismatches = [name for name, (pattern, output) in expected.items() if not re.search(pattern, output)]
    if mismatches:
        pins = (
            f"pip={PIP_VERSION}, Poetry={POETRY_VERSION}, "
            f"poetry-plugin-export={POETRY_EXPORT_VERSION}"
        )
        raise ToolchainVersionError(
            f"packaging toolchain mismatch for {', '.join(mismatches)}; expected {pins}"
        )


def main() -> int:
    try:
        check_packaging_toolchain()
    except ToolchainVersionError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(
        "Packaging toolchain verified: "
        f"pip {PIP_VERSION}, Poetry {POETRY_VERSION}, "
        f"poetry-plugin-export {POETRY_EXPORT_VERSION}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
