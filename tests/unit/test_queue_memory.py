from pathlib import Path
from unittest.mock import patch

import asyncio
import pytest

from scrapeyard.queue.browser_limiter import BrowserExecutionLimiter
from scrapeyard.queue.memory import get_process_rss_mb, memory_headroom_mb


def test_get_process_rss_mb_returns_none_off_linux() -> None:
    with patch("scrapeyard.queue.memory.sys.platform", "darwin"):
        assert get_process_rss_mb() is None


def test_get_process_rss_mb_parses_proc_statm(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc-self"
    proc_root.mkdir()
    (proc_root / "statm").write_text("50000 30000 1000 500 0 2000 0")

    with patch("scrapeyard.queue.memory.os.sysconf", return_value=4096):
        rss_mb = get_process_rss_mb(proc_root)

    assert rss_mb is not None
    assert round(rss_mb, 2) == round((30000 * 4096) / (1024 * 1024), 2)


@pytest.mark.parametrize("v2", [True, False])
def test_cgroup_headroom_includes_browsers_and_stricter_ancestors(tmp_path, v2):
    proc = tmp_path / "proc" / "self"
    proc.mkdir(parents=True)
    root = tmp_path / "cgroup"
    leaf = root / "service"
    leaf.mkdir(parents=True)
    (proc / "cgroup").write_text("0::/service\n" if v2 else "5:cpu,memory:/service\n")
    (proc / "mountinfo").write_text(
        f"1 0 0:1 / {root} rw - {'cgroup2 cgroup rw' if v2 else 'cgroup cgroup rw,memory'}\n"
    )
    usage, limit = ("memory.current", "memory.max") if v2 else ("memory.usage_in_bytes", "memory.limit_in_bytes")
    (leaf / usage).write_text(str(900 * 1024**2))
    (leaf / limit).write_text("max" if v2 else str(2**63 - 4096))
    (root / usage).write_text(str(1900 * 1024**2))
    (root / limit).write_text(str(2048 * 1024**2))
    with patch("scrapeyard.queue.memory.get_process_rss_mb", return_value=10):
        assert memory_headroom_mb(1024, proc) == 124
        assert memory_headroom_mb(4096, proc) == 148
        (leaf / usage).write_text(str(1100 * 1024**2))
        assert memory_headroom_mb(1024, proc) == -76
        (root / usage).write_text(str(2100 * 1024**2))
        assert memory_headroom_mb(4096, proc) == -52
        (leaf / limit).write_text("0")
        assert memory_headroom_mb(4096, proc) == -1100
        assert memory_headroom_mb(0, proc) == float("inf")


def test_memory_fallback_counts_descendants_and_tolerates_exited_child(tmp_path):
    proc = tmp_path / "proc"
    for pid, pages, children in (("self", 2560, "2 3"), ("2", 25600, "4"), ("4", 25600, "")):
        directory = proc / pid
        task = directory / "task" / "1"
        task.mkdir(parents=True)
        (directory / "statm").write_text(f"99999 {pages} 0")
        (task / "children").write_text(children)
    with patch("scrapeyard.queue.memory.os.sysconf", return_value=4096):
        assert memory_headroom_mb(200, proc / "self") == -10
    with patch("scrapeyard.queue.memory.sys.platform", "darwin"):
        assert memory_headroom_mb(200, proc / "self") == float("inf")


async def test_browser_memory_reservations_wait_resume_and_release_on_cancellation(monkeypatch):
    headroom = 600
    monkeypatch.setattr("scrapeyard.queue.browser_limiter.memory_headroom_mb", lambda _: headroom)
    limiter = BrowserExecutionLimiter(2, memory_limit_mb=1024, memory_reserve_mb=400)
    entered = asyncio.Event()

    async def enter():
        async with limiter.slot():
            entered.set()

    async with limiter.slot():
        waiting = asyncio.create_task(enter())
        await asyncio.sleep(0.01)
        assert not entered.is_set()  # Both starts cannot reserve the same 600 MiB.
        assert limiter.active == 1
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert limiter.active == 1
        waiting = asyncio.create_task(enter())
        await asyncio.sleep(0.01)
        assert not entered.is_set()
        headroom = 1000
        await asyncio.wait_for(waiting, timeout=2)
    assert entered.is_set()
    assert limiter.active == 0
