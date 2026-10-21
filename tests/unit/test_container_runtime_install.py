from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

INSTALLER = Path("scripts/install_container_runtime.sh")
DOCKER_KEY = "9DC858229FC7DD38854AE2D88D81803C0EBFCD88"

# Each stands in for a command the installer runs as root on a runner.
_SHIMS = {
    "id": 'echo "${FAKE_UID:-0}"',
    # The served key file, as gpg's colon listing of it: Docker's key with
    # its signing subkey, and whatever else the server put beside it.
    "curl": """
while [ "$#" -gt 1 ]; do shift; done
{
  echo "pub:-:4096:1:8D81803C0EBFCD88:"; echo "fpr:::::::::${FAKE_KEY}:"
  echo "sub:-:4096:1:7EA0A9C3F273FCD8:"; echo "fpr:::::::::D3306A018370199E527AE7A67EA0A9C3F273FCD8:"
  if [ -n "${FAKE_EXTRA_KEY:-}" ]; then
    echo "${FAKE_EXTRA_KIND:-pub}:-:4096:1:${FAKE_EXTRA_KEY: -16}:"; echo "fpr:::::::::${FAKE_EXTRA_KEY}:"
  fi
} > "$1"
""",
    # A keyring per --homedir: --import keeps the file, --export prints the
    # named key's records, --show-keys lists a file.
    "gpg": """
home=""; previous=""
for argument in "$@"; do
  [ "$previous" = "--homedir" ] && home="$argument"
  previous="$argument"
done
last="${@: -1}"
case " $* " in
  *" --import "*) cp "$last" "$home/imported" ;;
  *" --export "*)
    awk -F: -v want="$last" '
      $1 == "pub" || $1 == "sec" { keep = 0; held = $0; next }
      held != "" && $1 == "fpr" { keep = ($10 == want); if (keep) print held; held = "" }
      keep { print }' "$home/imported" ;;
  *" --show-keys "*) cat "$last" ;;
  *) echo "unexpected gpg $*" >&2; exit 64 ;;
esac
""",
    "gpgconf": ":",
    "dpkg": "echo amd64",
    "apt-get": 'echo "apt-get $*" >> "$SHIM_LOG"',
    "apt-cache": 'cat "$SHIM_DIR/madison-$2"',
    "systemctl": 'echo "systemctl $*" >> "$SHIM_LOG"',
    "sleep": ":",
    "containerd": 'echo "containerd containerd.io v${FAKE_CONTAINERD} 442cb34bda9a"',
    "docker": """
case "$*" in
  *Server.Version*) echo "$FAKE_ENGINE" ;;
  *Client.Version*) echo "$FAKE_CLIENT" ;;
  *RuncCommit*) echo "$FAKE_RUNC" ;;
  *DriverStatus*) echo "$FAKE_STORE" ;;
  "compose version --short") echo "$FAKE_COMPOSE" ;;
  *) echo "unexpected docker $*" >&2; exit 64 ;;
esac
""",
}

_MADISON = {
    "docker-ce": ("5:29.9.0-1~ubuntu.24.04~noble", "5:28.5.2-1~ubuntu.24.04~noble"),
    "docker-ce-cli": ("5:29.9.0-1~ubuntu.24.04~noble", "5:28.5.2-1~ubuntu.24.04~noble"),
    # The packaging revision is not always 1, and the newest comes first.
    "containerd.io": (
        "2.4.10-1~ubuntu.24.04~noble",
        "2.4.1-2~ubuntu.24.04~noble",
        "2.4.1-1~ubuntu.24.04~noble",
        "1.7.29-1~ubuntu.24.04~noble",
    ),
    "docker-compose-plugin": ("5.6.0-1~ubuntu.24.04~noble", "2.38.2-1~ubuntu.24.04~noble"),
}


def _pins() -> dict[str, str]:
    return dict(
        re.findall(r'^([A-Z]+)_VERSION="([^"]+)"$', INSTALLER.read_text(encoding="utf-8"), re.M)
    )


def _run(
    tmp_path: Path, *, offered: dict[str, tuple[str, ...]] | None = None, **overrides: str
) -> tuple[subprocess.CompletedProcess[str], str, Path]:
    shims = tmp_path / "shims"
    shims.mkdir()
    for name, body in _SHIMS.items():
        shim = shims / name
        shim.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
        shim.chmod(0o755)
    for package, versions in (offered or _MADISON).items():
        (shims / f"madison-{package}").write_text(
            "".join(f" {package} | {version} | https://repo noble/stable\n" for version in versions),
            encoding="utf-8",
        )
    apt = tmp_path / "apt"
    if not apt.exists():
        (apt / "sources.list.d").mkdir(parents=True)
    log = tmp_path / "commands.log"
    pins = _pins()
    env = {
        "PATH": f"{shims}{os.pathsep}{os.environ['PATH']}",
        "SHIM_DIR": str(shims),
        "SHIM_LOG": str(log),
        "SCRAPEYARD_RUNTIME_APT_DIR": str(apt),
        "SCRAPEYARD_RUNTIME_DOCKER_DIR": str(tmp_path / "docker"),
        "FAKE_STORE": "overlay2 [[Backing Filesystem extfs] [Supports d_type true]]",
        "FAKE_KEY": DOCKER_KEY,
        "FAKE_ENGINE": pins["DOCKER"],
        "FAKE_CLIENT": pins["DOCKER"],
        "FAKE_CONTAINERD": pins["CONTAINERD"],
        "FAKE_RUNC": f"v{pins['RUNC']}-0-g29dd3dc2",
        "FAKE_COMPOSE": pins["COMPOSE"],
        **overrides,
    }
    result = subprocess.run(
        [str(INSTALLER)], env=env, capture_output=True, text=True, timeout=30, check=False
    )
    return result, log.read_text(encoding="utf-8") if log.exists() else "", apt


def test_installer_installs_the_pinned_packages_and_checks_what_runs(tmp_path: Path) -> None:
    result, log, apt = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    install = next(line for line in log.splitlines() if line.startswith("apt-get install "))
    assert "docker-ce=5:29.9.0-1~ubuntu.24.04~noble" in install
    assert "docker-ce-cli=5:29.9.0-1~ubuntu.24.04~noble" in install
    # The newest packaging of the pinned version, and not 2.4.10 by prefix.
    assert "containerd.io=2.4.1-2~ubuntu.24.04~noble" in install
    assert "docker-compose-plugin=5.6.0-1~ubuntu.24.04~noble" in install
    assert "--allow-downgrades" in install
    assert log.index("apt-get update") < log.index("apt-get install")
    assert log.index("apt-get install") < log.index("systemctl restart containerd docker")
    source = (apt / "sources.list.d/docker.list").read_text(encoding="utf-8")
    assert f"signed-by={apt}/keyrings/docker.gpg" in source
    assert "https://download.docker.com/linux/ubuntu" in source
    assert "runc 1.5.2" in result.stdout
    assert "image store overlay2" in result.stdout


def test_installer_selects_the_classic_image_store_and_keeps_other_daemon_settings(
    tmp_path: Path,
) -> None:
    settings = tmp_path / "docker/daemon.json"
    settings.parent.mkdir()
    settings.write_text(
        '{"cgroup-parent": "/actions_job", "features": {"containerd-snapshotter": true, "cdi": true}}',
        encoding="utf-8",
    )

    result, log, _ = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    assert json.loads(settings.read_text(encoding="utf-8")) == {
        "cgroup-parent": "/actions_job",
        "features": {"containerd-snapshotter": False, "cdi": True},
    }
    # Docker reads the file when it starts, so it is written before the restart.
    assert log.index("apt-get install") < log.index("systemctl restart containerd docker")


def test_installer_writes_the_image_store_setting_where_docker_has_no_settings_yet(
    tmp_path: Path,
) -> None:
    result, _, _ = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / "docker/daemon.json").read_text(encoding="utf-8")) == {
        "features": {"containerd-snapshotter": False}
    }


def test_installer_reuses_a_repository_entry_the_image_already_has(tmp_path: Path) -> None:
    existing = tmp_path / "apt/sources.list.d/image-docker.list"
    existing.parent.mkdir(parents=True)
    existing.write_text(
        "deb [signed-by=/etc/apt/keyrings/docker.gpg] "
        "https://download.docker.com/linux/ubuntu noble stable\n",
        encoding="utf-8",
    )

    result, _, apt = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    # A second entry with another key path would make apt refuse both.
    assert [path.name for path in (apt / "sources.list.d").iterdir()] == ["image-docker.list"]


def test_installer_refuses_a_repository_key_that_is_not_dockers(tmp_path: Path) -> None:
    result, log, apt = _run(tmp_path, FAKE_KEY="0" * 40)

    assert result.returncode == 1
    assert "not Docker's release key" in result.stderr
    assert not (apt / "sources.list.d/docker.list").exists()
    assert not (apt / "keyrings").exists()
    assert "apt-get" not in log


@pytest.mark.parametrize("kind", ["pub", "sec"])
def test_installer_gives_apt_only_dockers_key_from_what_is_served(
    tmp_path: Path, kind: str
) -> None:
    # A key behind Docker's in the served file, public or secret, must not
    # reach the keyring apt trusts for the repository.
    other = "1" * 40
    result, _, apt = _run(tmp_path, FAKE_EXTRA_KEY=other, FAKE_EXTRA_KIND=kind)

    assert result.returncode == 0, result.stderr
    keyring = (apt / "keyrings/docker.gpg").read_text(encoding="utf-8")
    assert DOCKER_KEY in keyring
    assert other not in keyring


def test_installer_stops_when_the_repository_lacks_a_pinned_version(tmp_path: Path) -> None:
    offered = {**_MADISON, "docker-compose-plugin": ("2.38.2-1~ubuntu.24.04~noble",)}

    result, log, _ = _run(tmp_path, offered=offered)

    assert result.returncode == 1
    assert "does not offer docker-compose-plugin 5.6.0" in result.stderr
    assert "apt-get install" not in log


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"FAKE_ENGINE": "28.5.2"}, "Docker Engine is 28.5.2, not the pinned 29.9.0"),
        ({"FAKE_CLIENT": "28.5.2"}, "the Docker client is 28.5.2, not the pinned 29.9.0"),
        ({"FAKE_CONTAINERD": "1.7.29"}, "containerd is 1.7.29, not the pinned 2.4.1"),
        # A runc the image put ahead of the packaged one: a release can pass
        # under one runc and start no container under another.
        ({"FAKE_RUNC": "v1.3.3-0-gd842d771"}, "runc is 1.3.3, not the pinned 1.5.2"),
        ({"FAKE_RUNC": ""}, "runc is missing, not the pinned 1.5.2"),
        ({"FAKE_COMPOSE": "2.38.2"}, "Compose is 2.38.2, not the pinned 5.6.0"),
        # A release built here would be refused by a host, and a host's by this daemon.
        (
            {"FAKE_STORE": "overlayfs [[driver-type io.containerd.snapshotter.v1]]"},
            "Docker stores images with overlayfs [[driver-type io.containerd.snapshotter.v1]], "
            "not with its classic image store",
        ),
        ({"FAKE_STORE": ""}, "Docker stores images with nothing it reports"),
    ],
)
def test_installer_fails_when_what_runs_is_not_what_is_pinned(
    tmp_path: Path, override: dict[str, str], message: str
) -> None:
    result, _, _ = _run(tmp_path, **override)

    assert result.returncode == 1
    assert message in result.stderr


def test_installer_refuses_to_run_without_root(tmp_path: Path) -> None:
    result, log, _ = _run(tmp_path, FAKE_UID="1000")

    assert result.returncode == 1
    assert "run as root" in result.stderr
    assert log == ""


def test_deployment_guide_names_the_versions_the_lanes_install() -> None:
    pins = _pins()
    assert set(pins) == {"DOCKER", "CONTAINERD", "COMPOSE", "RUNC"}
    guide = Path("docs/DEPLOYMENT.md").read_text(encoding="utf-8")
    section = guide.split("## Container runtime versions", 1)[1].split("\n## ", 1)[0]
    assert f"| `docker-ce`, `docker-ce-cli` | {pins['DOCKER']} |" in section
    assert (
        f"| `containerd.io` | {pins['CONTAINERD']}, which carries runc {pins['RUNC']} |" in section
    )
    assert f"| `docker-compose-plugin` | {pins['COMPOSE']} |" in section


@pytest.mark.parametrize(
    ("workflow_file", "job_name", "first_use"),
    [
        ("release-rehearsal.yml", "release", "release_artifact.py create"),
        ("release-rehearsal.yml", "release-rehearsal", "run_deployment_rehearsal.sh"),
        (
            "container-browser-smoke.yml",
            "production-container-smoke",
            "run_container_browser_smoke.sh",
        ),
    ],
)
def test_container_lanes_replace_the_runner_runtime_before_they_build(
    workflow_file: str, job_name: str, first_use: str
) -> None:
    workflow: dict[str, Any] = yaml.load(
        (Path(".github/workflows") / workflow_file).read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    assert "scripts/install_container_runtime.sh" in workflow["on"]["pull_request"]["paths"]
    commands = [step.get("run", "") for step in workflow["jobs"][job_name]["steps"]]
    install = commands.index("sudo ./scripts/install_container_runtime.sh")
    assert install < next(i for i, command in enumerate(commands) if first_use in command)
    # The job log shows the runtime after the replacement, not before it.
    shown = next(i for i, command in enumerate(commands) if "runc --version" in command)
    assert install < shown
    assert "docker version" in commands[shown]
