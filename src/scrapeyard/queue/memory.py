"""Memory headroom for admission and browser execution."""

from __future__ import annotations

import ctypes
import gc
import os
import re
import sys
from collections.abc import Callable
from functools import cache
from pathlib import Path

from scrapeyard.runtime.metrics import MEMORY_HEADROOM

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


def _cgroup_memory(proc_root: Path) -> list[tuple[float, float]]:
    """Read usage/limit pairs from the process's v1/v2 memory hierarchy."""
    samples: list[tuple[float, float]] = []
    try:
        memberships = [line.split(":", 2) for line in (proc_root / "cgroup").read_text().splitlines()]
        mounts = (proc_root / "mountinfo").read_text().splitlines()
        for mount in mounts:
            before, after = mount.split(" - ", 1)
            fields, fs = before.split(), after.split()
            if fs[0] not in {"cgroup", "cgroup2"}:
                continue
            v2 = fs[0] == "cgroup2"
            if not v2 and "memory" not in fs[2].split(","):
                continue
            # mountinfo escapes spaces, tabs, newlines and backslashes as octal.
            root, mountpoint = (
                Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), part))
                for part in fields[3:5]
            )
            for _, controllers, member in memberships:
                if (v2 and controllers) or (not v2 and "memory" not in controllers.split(",")):
                    continue
                try:
                    relative = Path(member).relative_to(root)
                except ValueError:
                    continue
                directory = mountpoint / relative
                if ".." in relative.parts:
                    continue
                while True:
                    try:
                        usage = int((directory / ("memory.current" if v2 else "memory.usage_in_bytes")).read_text())
                        raw_limit = (directory / ("memory.max" if v2 else "memory.limit_in_bytes")).read_text().strip()
                        limit = float("inf") if raw_limit == "max" else int(raw_limit) / (1024 * 1024)
                        if usage >= 0 and limit >= 0:
                            samples.append((usage / (1024 * 1024), limit))
                    except (OSError, ValueError):
                        pass
                    if directory == mountpoint:
                        break
                    directory = directory.parent
    except (OSError, ValueError, IndexError):
        pass
    return samples


def _process_tree_rss_mb(proc_root: Path) -> float | None:
    """Conservative Linux fallback: RSS of this process and its descendants."""
    total = get_process_rss_mb(proc_root)
    if total is None:
        return None
    pending = [proc_root]
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        for children in current.glob("task/*/children"):
            try:
                pids = children.read_text().split()
            except OSError:
                continue
            for pid in pids:
                if not pid.isdecimal() or pid in seen:
                    continue
                seen.add(pid)
                child = proc_root.parent / pid
                total += get_process_rss_mb(child) or 0
                pending.append(child)
    return total


def memory_headroom_mb(limit_mb: int, proc_root: Path = _DEFAULT_PROC_ROOT) -> float:
    """Return remaining MiB, or infinity when disabled/unavailable.

    A positive service ceiling also respects stricter visible ancestor cgroup
    limits. Outside cgroups use process-tree RSS (shared pages count twice).
    """
    headroom = float("inf")
    if limit_mb > 0 and sys.platform.startswith("linux"):
        samples = _cgroup_memory(proc_root)
        if samples:
            # The first sample is the process's own group; ancestors may also
            # contain unrelated work, so apply the service ceiling only here.
            headroom = min(limit_mb - samples[0][0], *(limit - usage for usage, limit in samples))
        else:
            usage = _process_tree_rss_mb(proc_root)
            if usage is not None:
                headroom = limit_mb - usage
    MEMORY_HEADROOM.set(headroom * 1024 * 1024)
    return headroom


@cache
def _malloc_trim() -> Callable[[int], int] | None:
    """Return glibc's ``malloc_trim``, or ``None`` on other platforms and C libraries."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        trim = ctypes.CDLL(None).malloc_trim
    except (OSError, AttributeError):
        return None
    trim.argtypes = [ctypes.c_size_t]
    trim.restype = ctypes.c_int
    return trim


def freeze_startup_heap() -> None:
    """Keep objects built at startup out of every later garbage collection.

    Modules, settings, stores and the application live as long as the process.
    After one collection they move to the permanent generation, so collections
    during jobs and the one in :func:`release_memory` scan only newer objects.
    """
    gc.collect()
    gc.freeze()


def thaw_startup_heap() -> None:
    """Return frozen objects to normal collection when the service shuts down."""
    gc.unfreeze()


def release_memory() -> None:
    """Collect garbage and return freed heap pages to the operating system.

    Browsers and their drivers exit with each target, but the allocator keeps
    the heap pages freed after parsing pages and building results. Returning
    them brings an idle process back near its baseline for admission checks
    and the container limit.
    """
    gc.collect()
    trim = _malloc_trim()
    if trim is not None:
        trim(0)
