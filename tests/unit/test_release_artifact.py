from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import release_artifact


def test_release_verifies_content_and_rejects_drift_before_loading(tmp_path, monkeypatch):
    bundle = tmp_path / "release"
    bundle.mkdir()
    (bundle / "images.tar").write_bytes(b"retained image archive")
    images = {
        name: "sha256:" + value * 64
        for name, value in (("scrapeyard", "a"), ("redis", "b"), ("egress-probe", "c"))
    }
    overlay = {"services": {name: {"image": value} for name, value in images.items()}}
    (bundle / "images.json").write_text(json.dumps(overlay))
    manifest = {
        "format": "scrapeyard-release-v1",
        "revision": "d" * 40,
        "images": images,
        "files": release_artifact.inventory(bundle),
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    assert release_artifact.verify(bundle) == manifest
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(release_artifact, "run", lambda *args, **kwargs: args[-1])
    release_artifact.load_images(bundle)
    assert calls == [(["docker", "image", "load", "--input", str(bundle / "images.tar")],)]
    calls.clear()
    for change in ("content", "extra", "permission", "symlink", "incomplete"):
        image = bundle / "images.tar"
        if change == "content":
            image.write_bytes(b"corrupt")
        elif change == "extra":
            (bundle / "extra").touch()
        elif change == "permission":
            image.chmod(0o700)
        elif change == "symlink":
            image.unlink()
            image.symlink_to(bundle / "images.json")
        else:
            (bundle / "manifest.json").unlink()
        with pytest.raises((ValueError, OSError)):
            release_artifact.load_images(bundle)
        assert not calls
        if image.is_symlink():
            image.unlink()
        image.write_bytes(b"retained image archive")
        image.chmod(manifest["files"]["images.tar"]["mode"])
        (bundle / "extra").unlink(missing_ok=True)


@pytest.mark.parametrize("failure", ["disk", "scan", "qualification", "network", None])
def test_release_is_published_only_after_all_checks_pass(tmp_path, monkeypatch, failure):
    commands = []
    # The real check reads the temp filesystem, which may hold less than 15 GiB.
    free = (15 if failure == "disk" else 16) * 1024**3 - 1
    monkeypatch.setattr(
        "scripts.release_artifact.shutil.disk_usage", lambda _path: SimpleNamespace(free=free)
    )

    def execute(command, *, cwd, **kwargs):
        commands.append(command)
        if command[0] == "git":
            return original_run(command, cwd=cwd, **kwargs)
        if command[0] == {
            "scan": "./scripts/run_container_security_scan.sh",
            "qualification": "./scripts/run_release_qualification.sh",
        }.get(failure):
            raise subprocess.CalledProcessError(1, command)
        if command[:3] == ["docker", "image", "save"]:
            Path(command[4]).write_bytes(b"retained images")
            Path(command[4]).chmod(0o666)

    def output(*args, **kwargs):
        if args[0] == "git":
            return original_output(*args, **kwargs)
        if args[:3] == ("docker", "network", "ls"):
            return "host existing"
        if args[:3] == ("docker", "network", "inspect"):
            return json.dumps(
                [
                    {"Name": "host", "IPAM": {"Config": None}},
                    {
                        "Name": "active",
                        "IPAM": {
                            "Config": [
                                {
                                    "Subnet": "172.30.0.0/24"
                                    if failure == "network"
                                    else "172.19.0.0/24"
                                }
                            ]
                        },
                    },
                ]
            )
        if args[:2] == ("docker", "compose"):
            return json.dumps(
                {
                    "networks": {"backend": {"ipam": {"config": [{"subnet": "172.30.0.0/24"}]}}},
                    "services": {"redis": {"image": "redis"}, "egress-probe": {"image": "probe"}},
                }
            )
        return "sha256:" + "a" * 64

    original_run = subprocess.run
    original_output = release_artifact.run
    monkeypatch.setattr(subprocess, "run", execute)
    monkeypatch.setattr(release_artifact, "run", output)
    if failure is None:
        # A build account with a group-writable umask must not leak it into the release.
        previous_umask = os.umask(0o002)
        try:
            release_artifact.create(tmp_path / "candidate", "HEAD")
        finally:
            os.umask(previous_umask)
        manifest = release_artifact.verify(tmp_path / "candidate")
        assert set(manifest["images"]) == {"scrapeyard", "redis", "egress-probe"}
        assert {
            "images.tar",
            "source/docker-compose.yml",
            "source/security/seccomp/chromium.json",
        } <= manifest["files"].keys()
        # Root runs what a release holds, so only its owner may write to it.
        assert manifest["files"]["images.tar"]["mode"] == 0o644
        assert (tmp_path / "candidate" / "manifest.json").stat().st_mode & 0o777 == 0o644
        assert not any(entry["mode"] & 0o022 for entry in manifest["files"].values())
        assert not any(
            path.stat().st_mode & 0o022 for path in (tmp_path / "candidate").rglob("*")
        )
        return
    with pytest.raises((subprocess.CalledProcessError, ValueError)) as refused:
        release_artifact.create(tmp_path / "candidate", "HEAD")
    assert ("15 GiB free disk" in str(refused.value)) == (failure == "disk")
    assert not (tmp_path / "candidate").exists()
    assert not any(command[:3] == ["docker", "image", "save"] for command in commands)


@pytest.mark.parametrize("random_name", ["0brntqm_", "_abc1234", "ab___cd9", "________"])
def test_release_image_name_is_valid_whatever_the_temporary_directory_is_called(
    tmp_path, monkeypatch, random_name
):
    commands = []
    monkeypatch.setattr(
        "scripts.release_artifact.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=16 * 1024**3),
    )
    # tempfile draws names from letters, digits and the underscore.
    monkeypatch.setattr(
        "scripts.release_artifact.tempfile._get_candidate_names", lambda: iter([random_name])
    )

    def execute(command, *, cwd, **kwargs):
        commands.append(command)
        if command[0] == "git":
            return original_run(command, cwd=cwd, **kwargs)
        if command[:3] == ["docker", "image", "save"]:
            Path(command[4]).write_bytes(b"retained images")

    def output(*args, **kwargs):
        if args[0] == "git":
            return original_output(*args, **kwargs)
        if args[:3] == ("docker", "network", "ls"):
            return ""
        if args[:2] == ("docker", "compose"):
            return json.dumps(
                {
                    "networks": {},
                    "services": {"redis": {"image": "redis"}, "egress-probe": {"image": "probe"}},
                }
            )
        return "sha256:" + "a" * 64

    original_run = subprocess.run
    original_output = release_artifact.run
    monkeypatch.setattr(subprocess, "run", execute)
    monkeypatch.setattr(release_artifact, "run", output)

    release_artifact.create(tmp_path / "candidate", "HEAD")

    build = next(command for command in commands if command[:2] == ["docker", "build"])
    tag = build[build.index("--tag") + 1]
    # Docker's name grammar: runs of letters and digits joined by one of
    # ".", "_", "__" or hyphens, never an underscore beside a hyphen.
    assert re.fullmatch(r"[a-z0-9]+((\.|_|__|-+)[a-z0-9]+)*", tag), tag
    assert tag.startswith("scrapeyard-release-") and tag.endswith("-scrapeyard")


def test_secure_release_deploy_is_offline_and_stops_before_policy_changes(tmp_path):
    # Run the real shell flow with host mutations replaced by executable fakes.
    security = tmp_path / "security"
    security.mkdir()
    wrapper = security / "deploy-secure-compose.sh"
    text = Path("security/deploy-secure-compose.sh").read_text()
    for old, new in (
        ("((EUID == 0)) || fail", "true || fail"),
        ("LOCK_DIR=/run/scrapeyard", f"LOCK_DIR={tmp_path / 'lock'}"),
    ):
        assert text.count(old) == 1
        text = text.replace(old, new)
    wrapper.write_text(text)
    log = tmp_path / "calls"
    fake = tmp_path / "bin"
    fake.mkdir()
    docker = fake / "docker"
    docker.write_text("""#!/bin/bash
echo "docker $*" >> "$CALLS"
case "$*" in
  "compose config --images") echo sha256:aaaaaaaa ;;
  "image inspect "*) [[ "${MISSING_IMAGE:-}" != yes ]] ;;
  "compose ps -q egress-probe") echo probe ;;
  "inspect "*) echo abcdef123456 ;;
  "network inspect "*) echo br-release ;;
esac
""")
    docker.chmod(0o755)
    python = fake / "python3"
    python.write_text(
        '#!/bin/bash\necho "verify-load" >> "$CALLS"\n[[ "${INVALID_RELEASE:-}" != yes ]]\n'
    )
    python.chmod(0o755)
    bundle = tmp_path / "bundle"
    source_security = bundle / "source" / "security"
    source_security.mkdir(parents=True)
    for name in ("install-docker-egress-policy.sh", "install-chromium-apparmor-profile.sh"):
        installer = source_security / name
        installer.write_text(
            f'#!/bin/bash\necho "{name}" >> "$CALLS"\n[[ "$PYTHONDONTWRITEBYTECODE" == 1 && "${{FAIL_POLICY:-}}" != yes ]]\n'
        )
        installer.chmod(0o755)
    env = dict(
        os.environ,
        PATH=f"{fake}:{os.environ['PATH']}",
        CALLS=str(log),
        SCRAPEYARD_PROXY_URL="https://proxy.example",
        COMPOSE_PROJECT_NAME="existing",
    )
    command = ["bash", str(wrapper), "--release", str(bundle)]
    subprocess.run(command, cwd=tmp_path, env=env, check=True, capture_output=True)
    calls = log.read_text().splitlines()
    assert not any("build scrapeyard" in call for call in calls)
    assert calls.index("verify-load") < calls.index("docker compose stop scrapeyard")
    assert (
        calls.index("docker compose stop scrapeyard")
        < calls.index("docker compose up -d --wait --no-build --pull never egress-probe redis")
        < calls.index("install-docker-egress-policy.sh")
        < calls.index("docker compose up -d --wait --no-build --pull never")
    )
    for failure in ("INVALID_RELEASE", "MISSING_IMAGE", "FAIL_POLICY"):
        log.write_text("")
        result = subprocess.run(
            command, cwd=tmp_path, env=dict(env, **{failure: "yes"}), capture_output=True
        )
        assert result.returncode != 0
        calls = log.read_text()
        assert "docker compose up -d --wait --no-build --pull never\n" not in calls
        if failure != "FAIL_POLICY":
            assert "docker compose stop scrapeyard" not in calls
        else:
            assert "the application is left stopped" in result.stderr.decode()


def _wrapper_with_fake_docker(tmp_path, docker_script):
    security = tmp_path / "security"
    security.mkdir()
    wrapper = security / "deploy-secure-compose.sh"
    text = Path("security/deploy-secure-compose.sh").read_text()
    for old, new in (
        ("((EUID == 0)) || fail", "true || fail"),
        ("LOCK_DIR=/run/scrapeyard", f"LOCK_DIR={tmp_path / 'lock'}"),
        ("flock --wait 600 9", "flock --nonblock 9"),
    ):
        assert text.count(old) == 1
        text = text.replace(old, new)
    wrapper.write_text(text)
    fake = tmp_path / "bin"
    fake.mkdir()
    docker = fake / "docker"
    docker.write_text(docker_script)
    docker.chmod(0o755)
    python = fake / "python3"
    python.write_text("#!/bin/bash\ntrue\n")
    python.chmod(0o755)
    bundle = tmp_path / "bundle"
    source_security = bundle / "source" / "security"
    source_security.mkdir(parents=True)
    for name in ("install-docker-egress-policy.sh", "install-chromium-apparmor-profile.sh"):
        installer = source_security / name
        installer.write_text("#!/bin/bash\ntrue\n")
        installer.chmod(0o755)
    env = dict(
        os.environ,
        PATH=f"{fake}:{os.environ['PATH']}",
        CALLS=str(tmp_path / "calls"),
        SCRAPEYARD_PROXY_URL="https://proxy.example",
        COMPOSE_PROJECT_NAME="existing",
    )
    return ["bash", str(wrapper), "--release", str(bundle)], env


_FAKE_DOCKER = """#!/bin/bash
echo "docker $*" >> "$CALLS"
case "$*" in
  "compose config --images") echo sha256:aaaaaaaa ;;
  "compose ps -q egress-probe") echo probe ;;
  "inspect "*) echo abcdef123456 ;;
  "network inspect "*) echo br-release ;;
  "compose up -d --wait --no-build --pull never") exit "${START_STATUS:-0}" ;;
esac
"""


def test_a_start_that_fails_leaves_the_application_stopped_not_restarting(tmp_path):
    command, env = _wrapper_with_fake_docker(tmp_path, _FAKE_DOCKER)

    result = subprocess.run(command, cwd=tmp_path, env=dict(env, START_STATUS="1"), capture_output=True)

    assert result.returncode == 1
    calls = Path(env["CALLS"]).read_text().splitlines()
    # The container is unless-stopped: left alone it would keep restarting.
    assert calls[-1] == "docker compose stop scrapeyard"
    assert calls[-2] == "docker compose up -d --wait --no-build --pull never"
    assert b"the application is left stopped" in result.stderr

    Path(env["CALLS"]).write_text("")
    subprocess.run(command, cwd=tmp_path, env=env, check=True, capture_output=True)
    assert Path(env["CALLS"]).read_text().splitlines()[-1] == (
        "docker compose up -d --wait --no-build --pull never"
    )


def test_a_second_deployment_waits_for_the_first_instead_of_interleaving(tmp_path):
    command, env = _wrapper_with_fake_docker(tmp_path, _FAKE_DOCKER)
    lock_dir = tmp_path / "lock"
    lock_dir.mkdir()
    with (lock_dir / "deploy.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True)

    assert result.returncode == 2
    assert b"another deployment" in result.stderr
    # The lock is taken before the release is loaded or anything is stopped.
    assert Path(env["CALLS"]).read_text().splitlines() == ["docker compose version"]
    subprocess.run(command, cwd=tmp_path, env=env, check=True, capture_output=True)
