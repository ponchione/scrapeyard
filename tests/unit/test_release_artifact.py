from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

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


@pytest.mark.parametrize("failure", ["scan", "qualification", "network", None])
def test_release_is_published_only_after_all_checks_pass(tmp_path, monkeypatch, failure):
    commands = []

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
        release_artifact.create(tmp_path / "candidate", "HEAD")
        manifest = release_artifact.verify(tmp_path / "candidate")
        assert set(manifest["images"]) == {"scrapeyard", "redis", "egress-probe"}
        assert {
            "images.tar",
            "source/docker-compose.yml",
            "source/security/seccomp/chromium.json",
        } <= manifest["files"].keys()
        return
    with pytest.raises((subprocess.CalledProcessError, ValueError)):
        release_artifact.create(tmp_path / "candidate", "HEAD")
    assert not (tmp_path / "candidate").exists()
    assert not any(command[:3] == ["docker", "image", "save"] for command in commands)


def test_secure_release_deploy_is_offline_and_stops_before_policy_changes(tmp_path):
    # Run the real shell flow with host mutations replaced by executable fakes.
    security = tmp_path / "security"
    security.mkdir()
    wrapper = security / "deploy-secure-compose.sh"
    text = Path("security/deploy-secure-compose.sh").read_text()
    wrapper.write_text(text.replace("((EUID == 0)) || fail", "true || fail"))
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
