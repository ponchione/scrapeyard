"""Platform-aware process memory helpers for queue admission checks."""

from __future__ import annotations

import os
import sys
from pathlib import Path


_DEFAULT_PROC_ROOT = Path("/proc/self")


def get_process_rss_mb(proc_root: Path = _DEFAULT_PROC_ROOT) -> float | None:
    """Return current RSS in MB on Linux, or ``None`` if unavailable."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        statm = (proc_root / "statm").read_text()
        rss_pages = int(statm.split()[1])
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (OSError, IndexError, ValueError):
        return None
    return (rss_pages * page_size) / (1024 * 1024)
