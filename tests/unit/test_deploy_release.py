from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path("security/deploy-release.sh")
OLD = "scrapeyard-release-" + "a" * 40
NEW_COMMIT = "b" * 40
NEW = f"scrapeyard-release-{NEW_COMMIT}"

pytestmark = pytest.mark.skipif(shutil.which("zstd") is None, reason="tar --zstd needs zstd")


class _Host:
    """deploy-release.sh with its fixed paths moved, and stand-ins for the host's tools."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.calls = tmp_path / "calls"
        self.releases = tmp_path / "releases"
        self.current = self.releases / "current"
        self.state = tmp_path / "state"
        self.health_queue = tmp_path / "health-queue"
        self.bin = tmp_path / "bin"
        for directory in (self.releases, self.state, self.bin, tmp_path / "work", tmp_path / "lock"):
            directory.mkdir()
        text = SCRIPT.read_text(encoding="utf-8")
        for old, new in (
            ("((EUID == 0)) || fail", "true || fail"),
            ("RELEASES=/srv/scrapeyard-releases", f"RELEASES={self.releases}"),
            ("CURRENT=/srv/scrapeyard-releases/current", f"CURRENT={self.current}"),
            ("WORK_ROOT=/var/backups/scrapeyard", f"WORK_ROOT={tmp_path / 'work'}"),
            ("LOCK_DIR=/run/scrapeyard", f"LOCK_DIR={tmp_path / 'lock'}"),
            ("BACKUP_STATUS=/var/lib/scrapeyard/backup-status.json", f"BACKUP_STATUS={self.state / 'backup.json'}"),
            ("HEALTH_STATUS=/var/lib/scrapeyard/health-status.json", f"HEALTH_STATUS={self.state / 'health.json'}"),
            ("RELEASE_TOOL=/usr/local/sbin/scrapeyard-release", f"RELEASE_TOOL={self.bin / 'scrapeyard-release'}"),
            ("UPLOAD_TOOL=/usr/local/sbin/scrapeyard-release-upload", f"UPLOAD_TOOL={self.bin / 'upload'}"),
            ("HEALTH_PAUSE=30", "HEALTH_PAUSE=0"),
        ):
            assert text.count(old) == 1, old
            text = text.replace(old, new)
        self.script = tmp_path / "deploy-release.sh"
        self.script.write_text(text, encoding="utf-8")
        self.script.chmod(0o755)
        # The host check passes unless the queue's next line says otherwise.
        self.tool(
            "systemctl",
            'echo "systemctl $*" >> "$CALLS"\n'
            'now=$(date -u +%Y-%m-%dT%H:%M:%SZ)\n'
            'case "$2" in\n'
            '  scrapeyard-health.service)\n'
            '    status=ok; if [[ -s "$HEALTH_QUEUE" ]]; then status=$(head -n 1 "$HEALTH_QUEUE"); sed -i 1d "$HEALTH_QUEUE"; fi\n'
            '    printf \'{"checked_at":"%s","status":"%s","results":[{"name":"edge","ok":%s}]}\' "$now" "$status" '
            '"$([[ $status == ok ]] && echo true || echo false)" > "$STATE/health.json" ;;\n'
            '  scrapeyard-backup.service)\n'
            '    [[ "${BACKUP_FAILS:-}" != yes ]] || exit 1\n'
            '    printf \'{"completed_at":"%s","uploaded":%s,"release":"%s","archive":"/b/x.age"}\' "$now" '
            '"${BACKUP_UPLOADED:-true}" "$(basename "$(readlink "$CURRENT")")" > "$STATE/backup.json" ;;\n'
            "esac\n",
        )
        self.tool(
            "scrapeyard-release",
            'echo "promote $2" >> "$CALLS"\n'
            '[[ "$2" != "${PROMOTE_FAILS:-none}" ]] || exit 2\n'
            'ln -sfn "$2" "$CURRENT"\n',
        )
        self.tool("upload", 'echo "upload $*" >> "$CALLS"\n')
        self.env = dict(
            os.environ,
            PATH=f"{self.bin}:{os.environ['PATH']}",
            CALLS=str(self.calls),
            STATE=str(self.state),
            CURRENT=str(self.current),
            HEALTH_QUEUE=str(self.health_queue),
        )
        self.release(self.releases / OLD, "a")
        self.current.symlink_to(self.releases / OLD)

    def tool(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text("#!/bin/bash\nset -eu\n" + body)
        path.chmod(0o755)

    @staticmethod
    def release(path: Path, revision_char: str, sql: str = "CREATE TABLE jobs (id);\n") -> None:
        (path / "source" / "sql").mkdir(parents=True)
        (path / "source" / "scripts").mkdir()
        (path / "source" / "sql" / "001.sql").write_text(sql)
        (path / "manifest.json").write_text(json.dumps({"revision": revision_char * 40}))
        # The verifier: refuses a release that holds a file named UNSCANNED.
        (path / "source" / "scripts" / "release_artifact.py").write_text(
            "import pathlib, sys\n"
            "open(__import__('os').environ['CALLS'], 'a').write(f'verify {sys.argv[1:]}\\n')\n"
            "sys.exit(1 if (pathlib.Path(sys.argv[-1]) / 'UNSCANNED').exists() else 0)\n"
        )

    def archive(self, sql: str = "CREATE TABLE jobs (id);\n", extra: str | None = None) -> tuple[Path, str]:
        build = self.root / "build"
        self.release(build / NEW, "b", sql)
        archive = self.root / f"{NEW}.tar.zst"
        plain = self.root / "plain.tar"
        with tarfile.open(plain, "w") as tar:
            tar.add(build / NEW, arcname=NEW)
            if extra:
                tar.add(build / NEW / "manifest.json", arcname=extra)
        subprocess.run(["zstd", "-q", "-f", str(plain), "-o", str(archive)], check=True)
        return archive, hashlib.sha256(archive.read_bytes()).hexdigest()

    def run(self, archive: Path, checksum: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.script), str(archive), checksum],
            env={**self.env, **env},
            capture_output=True,
            text=True,
            check=False,
        )

    def log(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []


@pytest.fixture
def host(tmp_path: Path) -> _Host:
    return _Host(tmp_path)


def _acts(host: _Host) -> list[str]:
    """The calls that change something, in order."""
    return [
        line
        for line in host.log()
        if line.startswith(("promote", "upload", "systemctl start scrapeyard-backup"))
    ]


def test_deploys_after_a_stored_backup_then_keeps_both_releases_off_the_host(host: _Host) -> None:
    archive, checksum = host.archive()
    result = host.run(archive, checksum)
    assert result.returncode == 0, result.stderr
    new = host.releases / NEW
    assert os.readlink(host.current) == str(new)
    # The running release's verifier judges the candidate, with its scans.
    assert f"verify ['verify', '--scanned', '{new}']" in host.log()
    acts = _acts(host)
    assert acts[0] == "systemctl start scrapeyard-backup.service"
    assert acts[1] == f"promote {new}"
    # The new release goes to the store as the bytes whose checksum was given.
    assert acts[2].startswith(f"upload {NEW} ") and acts[2].endswith(f"/{NEW}.tar.zst")
    assert acts[3] == f"upload {OLD}"
    # The host check ran before anything and again after the promotion.
    checks = [i for i, line in enumerate(host.log()) if "scrapeyard-health" in line]
    assert checks[0] < host.log().index(acts[0]) and checks[-1] > host.log().index(acts[1])


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("checksum", "does not have the checksum given"),
        ("outside", "holds something outside"),
        ("sql", "changes the SQL"),
        ("unscanned", "fails verification"),
        ("unhealthy", "host check fails before anything was changed"),
        ("backup-fails", "the backup failed"),
        ("not-uploaded", "did not reach the store"),
    ],
)
def test_refuses_before_promoting_and_leaves_the_running_release(
    host: _Host, change: str, expected: str
) -> None:
    archive, checksum = host.archive(
        sql="CREATE TABLE jobs (id, more);\n" if change == "sql" else "CREATE TABLE jobs (id);\n",
        extra=f"{NEW}/../escape" if change == "outside" else None,
    )
    env: dict[str, str] = {}
    if change == "checksum":
        checksum = "0" * 64
    elif change == "unscanned":
        # Unpacked by an earlier attempt with the archive's manifest, then marked.
        host.run(archive, checksum, BACKUP_FAILS="yes")
        (host.releases / NEW / "UNSCANNED").write_text("")
        host.calls.unlink()
    elif change == "unhealthy":
        host.health_queue.write_text("fail\n")
    elif change == "backup-fails":
        env["BACKUP_FAILS"] = "yes"
    elif change == "not-uploaded":
        env["BACKUP_UPLOADED"] = "false"
    result = host.run(archive, checksum, **env)
    assert result.returncode == 2
    assert expected in result.stderr
    assert os.readlink(host.current) == str(host.releases / OLD)
    assert not [line for line in host.log() if line.startswith(("promote", "upload"))]
    if change in ("checksum", "outside", "unhealthy"):
        assert not (host.releases / NEW).exists()
        assert not [line for line in host.log() if "backup" in line]


def test_a_candidate_the_host_check_refuses_is_rolled_back_and_not_uploaded(host: _Host) -> None:
    archive, checksum = host.archive()
    # Passes before; fails every attempt after the promotion; passes after the rollback.
    host.health_queue.write_text("ok\n" + "fail\n" * 6 + "ok\n")
    result = host.run(archive, checksum)
    assert result.returncode == 1
    assert "promoted again and the host check passes" in result.stderr
    assert "edge" in result.stderr
    assert [line for line in _acts(host) if line.startswith("promote")] == [
        f"promote {host.releases / NEW}",
        f"promote {host.releases / OLD}",
    ]
    assert os.readlink(host.current) == str(host.releases / OLD)
    assert not [line for line in host.log() if line.startswith("upload")]


def test_a_failed_promotion_that_left_the_old_release_well_is_not_promoted_again(host: _Host) -> None:
    archive, checksum = host.archive()
    result = host.run(archive, checksum, PROMOTE_FAILS=str(host.releases / NEW))
    assert result.returncode == 1
    assert "still runs and the host check passes" in result.stderr
    assert [line for line in _acts(host) if line.startswith("promote")] == [f"promote {host.releases / NEW}"]
