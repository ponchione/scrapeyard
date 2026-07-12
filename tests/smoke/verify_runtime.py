"""Validate browser assets, privilege drop, writable paths, and smoke artifacts."""

from __future__ import annotations

import importlib.metadata
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def _chromium_revision(module: Any) -> str:
    browsers = Path(module.__file__).parent / "driver" / "package" / "browsers.json"
    payload = json.loads(browsers.read_text(encoding="utf-8"))
    return str(next(item["revision"] for item in payload["browsers"] if item["name"] == "chromium"))


def _pid_one_uid() -> int:
    for line in Path("/proc/1/status").read_text(encoding="utf-8").splitlines():
        if line.startswith("Uid:"):
            return int(line.split()[1])
    raise AssertionError("PID 1 status omitted Uid")


def _status_value(name: str) -> str:
    prefix = f"{name}:"
    for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"process status omitted {name}")


def _assert_writable(path: Path) -> None:
    assert path.is_dir(), f"required runtime directory is missing: {path}"
    with tempfile.NamedTemporaryFile(prefix=".item14-write-", dir=path, delete=True) as probe:
        probe.write(b"runtime-user-write-ok")
        probe.flush()


def main() -> None:
    import playwright
    import rebrowser_playwright
    from camoufox.pkgman import Version, launch_path

    uid = os.getuid()
    gid = os.getgid()
    assert uid != 0, "runtime verification did not execute as a non-root user"
    assert _pid_one_uid() == uid, "the uvicorn/worker PID is not running as the runtime user"
    assert Path.home() == Path("/home/scrapeyard")

    required = [
        Path("/data/db"),
        Path("/data/results"),
        Path("/data/adaptive"),
        Path("/data/logs"),
        Path.home() / ".cache",
        Path.home() / ".config",
        Path.home() / ".local",
        Path.home() / ".camoufox",
        Path("/tmp"),
    ]
    for path in required:
        path.mkdir(parents=True, exist_ok=True)
        _assert_writable(path)
        owner = path.stat()
        if path == Path("/tmp"):
            assert owner.st_uid == 0 and owner.st_gid == 0
            assert owner.st_mode & 0o1000, "/tmp is missing the sticky bit"
        else:
            assert owner.st_uid == uid and owner.st_gid == gid, (
                f"unexpected ownership for {path}"
            )

    playwright_version = importlib.metadata.version("playwright")
    rebrowser_version = importlib.metadata.version("rebrowser-playwright")
    camoufox_package_version = importlib.metadata.version("camoufox")
    playwright_revision = _chromium_revision(playwright)
    rebrowser_revision = _chromium_revision(rebrowser_playwright)

    browser_root = Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"])
    stock_browser = browser_root / f"chromium-{playwright_revision}"
    assert stock_browser.is_dir(), f"locked Playwright Chromium is missing: {stock_browser}"

    bundled_sandbox = (
        browser_root
        / f"chromium-{rebrowser_revision}"
        / "chrome-linux"
        / "chrome_sandbox"
    )
    assert bundled_sandbox.is_file() and os.access(bundled_sandbox, os.X_OK)
    assert not bundled_sandbox.stat().st_mode & 0o4000
    assert "CHROME_DEVEL_SANDBOX" not in os.environ

    camoufox_executable = Path(launch_path())
    assert camoufox_executable.is_file() and os.access(camoufox_executable, os.X_OK)
    camoufox_version = Version.from_path()
    cache_root = Path(os.environ["XDG_CACHE_HOME"])
    assert cache_root == Path("/opt/scrapeyard-cache")
    assert not os.access(cache_root, os.W_OK), "immutable browser cache is writable"
    ubo_manifest = cache_root / "camoufox" / "addons" / "UBO" / "manifest.json"
    assert json.loads(ubo_manifest.read_text(encoding="utf-8"))["version"] == "1.72.2"

    root_mount = next(
        line for line in Path("/proc/mounts").read_text(encoding="utf-8").splitlines()
        if line.split()[1] == "/"
    )
    assert "ro" in root_mount.split()[3].split(","), "container root filesystem is writable"
    assert _status_value("CapEff") == "0000000000000000"
    assert _status_value("CapBnd") == "0000000000040000"
    assert _status_value("NoNewPrivs") == "1"
    assert int(_status_value("Seccomp")) == 2
    assert Path("/proc/self/attr/current").read_text(encoding="utf-8").strip() == (
        "scrapeyard-chromium (enforce)"
    )

    result_root = Path("/data/results/item14-smoke")
    result_files = sorted(result_root.rglob("results.json"))
    screenshots = sorted(result_root.rglob("*.png"))
    assert len(result_files) >= 7, f"expected focused result artifacts, found {len(result_files)}"
    assert len(screenshots) >= 4, f"expected browser screenshots, found {len(screenshots)}"
    for artifact in [*result_files, *screenshots]:
        artifact_stat = artifact.stat()
        assert artifact_stat.st_uid == uid and artifact_stat.st_gid == gid
    for screenshot in screenshots:
        assert screenshot.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    report = {
        "runtime": {
            "uid": uid,
            "gid": gid,
            "pid_one_uid": _pid_one_uid(),
            "home": str(Path.home()),
            "writable_paths": [str(path) for path in required],
            "root_filesystem": "read-only",
            "effective_capabilities": _status_value("CapEff"),
            "capability_bounding_set": _status_value("CapBnd"),
            "no_new_privileges": int(_status_value("NoNewPrivs")),
            "seccomp_mode": int(_status_value("Seccomp")),
            "apparmor_profile": Path("/proc/self/attr/current").read_text(
                encoding="utf-8"
            ).strip(),
        },
        "browser_assets": {
            "playwright_version": playwright_version,
            "playwright_chromium_revision": playwright_revision,
            "playwright_chromium_path": str(stock_browser),
            "rebrowser_playwright_version": rebrowser_version,
            "rebrowser_chromium_revision": rebrowser_revision,
            "bundled_sandbox_path": str(bundled_sandbox),
            "sandbox_strategy": "unprivileged-user-namespace",
            "camoufox_package_version": camoufox_package_version,
            "camoufox_browser_version": camoufox_version.version,
            "camoufox_browser_release": camoufox_version.release,
            "camoufox_executable": str(camoufox_executable),
            "ubo_version": "1.72.2",
        },
        "artifacts": {
            "result_files": len(result_files),
            "screenshots": len(screenshots),
            "screenshot_bytes": sum(path.stat().st_size for path in screenshots),
        },
    }
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
