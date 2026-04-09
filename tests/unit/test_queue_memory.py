from pathlib import Path
from unittest.mock import patch

from scrapeyard.queue.memory import get_process_rss_mb


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
