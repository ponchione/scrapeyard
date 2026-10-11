#!/usr/bin/env python3
"""Reads Docker's record of the egress probe's health checks and fails a slow one.

The check runs every two seconds under a quarter of a CPU with a three-second
deadline. On this runner a check that takes most of that deadline still passes;
on a host with slower cores it does not, and no deployment starts there. The
fastest of the recorded checks is compared, so one disturbed run does not fail
the rehearsal while a check that became expensive does.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from typing import Any

LIMIT_SECONDS = 0.5
_MOMENT = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?")


def _seconds(moment: str) -> float:
    # Docker writes nanoseconds, which datetime does not read. Both ends of a
    # check carry the same zone, so it is left out.
    match = _MOMENT.match(moment)
    if match is None:
        raise ValueError(f"not a Docker timestamp: {moment!r}")
    whole = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S").timestamp()
    return whole + float(f"0.{match.group(2) or 0}")


def fastest_seconds(log: list[dict[str, Any]]) -> float:
    if not log:
        raise ValueError("Docker has recorded no health check")
    return min(_seconds(entry["End"]) - _seconds(entry["Start"]) for entry in log)


def main() -> None:
    try:
        fastest = fastest_seconds(json.load(sys.stdin))
    except (KeyError, TypeError, ValueError) as exc:
        print(f"probe health: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"{fastest:.3f}")
    if fastest >= LIMIT_SECONDS:
        print(
            f"probe health: the fastest recorded check took {fastest:.2f}s, "
            f"over the {LIMIT_SECONDS}s this runner allows",
            file=sys.stderr,
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
