from __future__ import annotations

import fcntl
import io
import json
import os
import re
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import release_artifact


def _image_archive(path: Path, images, *, store: str = "classic") -> None:
    """Write what `docker image save` writes for *images*, under either store."""
    hexes = [image.removeprefix("sha256:") for image in images]
    if store == "classic":
        manifest = [{"Config": f"blobs/sha256/{value}", "Layers": []} for value in hexes]
        index = {"manifests": [{"digest": "sha256:" + "f" * 64}]}
    else:
        manifest = [{"Config": "blobs/sha256/" + "f" * 64, "Layers": []} for _ in hexes]
        index = {"manifests": [{"digest": f"sha256:{value}"} for value in hexes]}
    with tarfile.open(path, "w") as archive:
        for name, payload in (("index.json", index), ("manifest.json", manifest)):
            data = json.dumps(payload).encode()
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))


def test_release_verifies_content_and_rejects_drift_before_loading(tmp_path, monkeypatch):
    bundle = tmp_path / "release"
    bundle.mkdir()
    images = {
        name: "sha256:" + value * 64
        for name, value in (("scrapeyard", "a"), ("redis", "b"), ("egress-probe", "c"))
    }
    _image_archive(bundle / "images.tar", images.values())
    retained = (bundle / "images.tar").read_bytes()
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
    # The containerd image store identifies images by their index entries.
    _image_archive(bundle / "images.tar", images.values(), store="containerd")
    manifest["files"] = release_artifact.inventory(bundle)
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    assert release_artifact.verify(bundle) == manifest
    # An archive that lacks an image is refused, though its checksum matches.
    _image_archive(bundle / "images.tar", [images["scrapeyard"], images["egress-probe"]])
    manifest["files"] = release_artifact.inventory(bundle)
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="images.tar does not hold the redis image"):
        release_artifact.verify(bundle)
    (bundle / "images.tar").write_bytes(b"not an archive")
    manifest["files"] = release_artifact.inventory(bundle)
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="images.tar is not an image archive"):
        release_artifact.verify(bundle)
    (bundle / "images.tar").write_bytes(retained)
    manifest["files"] = release_artifact.inventory(bundle)
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    assert release_artifact.verify(bundle) == manifest
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(release_artifact, "run", lambda *args, **kwargs: args[-1])
    release_artifact.load_images(bundle)
    assert calls == [(["docker", "image", "load", "--input", str(bundle / "images.tar")],)]
    calls.clear()

    # Under another image store the archive loads and its images get other
    # identifiers. The refusal has to say that, not only "No such image".
    def no_such_image(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(release_artifact, "run", no_such_image)
    with pytest.raises(ValueError, match="identifier depends on Docker's image store"):
        release_artifact.load_images(bundle)
    monkeypatch.setattr(release_artifact, "run", lambda *args, **kwargs: "sha256:" + "e" * 64)
    with pytest.raises(ValueError, match="loaded image identity differs"):
        release_artifact.load_images(bundle)
    monkeypatch.setattr(release_artifact, "run", lambda *args, **kwargs: args[-1])
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
        image.write_bytes(retained)
        image.chmod(manifest["files"]["images.tar"]["mode"])
        (bundle / "extra").unlink(missing_ok=True)


def test_a_release_to_ship_needs_a_scan_report_naming_each_image(tmp_path):
    images = {
        name: "sha256:" + value * 64
        for name, value in (("scrapeyard", "a"), ("redis", "b"), ("egress-probe", "c"))
    }
    manifest = {"images": images}
    reports = tmp_path / "security-reports"
    reports.mkdir()
    (reports / "vulnerabilities.json").write_text(
        json.dumps({"ArtifactName": images["scrapeyard"], "Results": []})
    )
    # The scanner before TD-99 read only its first argument: it wrote the
    # application's report and none for the images beside it.
    with pytest.raises(ValueError, match="no vulnerability report for its redis image"):
        release_artifact.verify_scanned(tmp_path, manifest)
    (reports / "vulnerabilities-dependency-1.json").write_text(
        json.dumps({"ArtifactName": "redis:7.4.11-alpine", "Metadata": {"ImageID": images["redis"]}})
    )
    (reports / "vulnerabilities-dependency-2.json").write_text(
        json.dumps({"ArtifactName": images["redis"]})
    )
    with pytest.raises(ValueError, match="not a scan of the egress-probe image"):
        release_artifact.verify_scanned(tmp_path, manifest)
    (reports / "vulnerabilities-dependency-2.json").write_text(
        json.dumps({"ArtifactName": images["egress-probe"]})
    )
    release_artifact.verify_scanned(tmp_path, manifest)


@pytest.mark.parametrize("failure", ["disk", "scan", "qualification", "network", "archive", None])
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
            # A save list that leaves an image out still writes an archive.
            saved = command[5:-1] if failure == "archive" else command[5:]
            _image_archive(Path(command[4]), saved)
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
        return dependency_ids.get(args[-1], "sha256:" + "a" * 64)

    dependency_ids = {"redis": "sha256:" + "b" * 64, "probe": "sha256:" + "c" * 64}
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
        assert manifest["images"] == {
            "scrapeyard": "sha256:" + "a" * 64,
            "redis": dependency_ids["redis"],
            "egress-probe": dependency_ids["probe"],
        }
        # Every image the release ships passes the scan, not only the application's.
        scans = [c for c in commands if c[0] == "./scripts/run_container_security_scan.sh"]
        assert scans == [["./scripts/run_container_security_scan.sh", *manifest["images"].values()]]
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
    if failure == "archive":
        assert "images.tar does not hold the egress-probe image" in str(refused.value)
        return
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
            _image_archive(Path(command[4]), command[5:])

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
  "info --format "*) [[ "${DOCKER_DOWN:-}" != yes ]] || { echo "[]"; exit 1; }
    [[ "${NO_APPARMOR:-}" == yes ]] || echo "[name=apparmor,profile=default name=seccomp,profile=builtin]" ;;
  "compose exec -T scrapeyard cat /proc/1/attr/current") echo "${APP_LABEL-scrapeyard-chromium (enforce)}" ;;
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
    # A host with no AppArmor, or a daemon that does not answer, is refused
    # before anything is stopped.
    for failure in ("INVALID_RELEASE", "MISSING_IMAGE", "FAIL_POLICY", "NO_APPARMOR", "DOCKER_DOWN"):
        log.write_text("")
        result = subprocess.run(
            command, cwd=tmp_path, env=dict(env, **{failure: "yes"}), capture_output=True
        )
        assert result.returncode != 0
        calls = log.read_text()
        assert "docker compose up -d --wait --no-build --pull never\n" not in calls
        if failure == "NO_APPARMOR":
            assert "Docker reports no AppArmor on this host" in result.stderr.decode()
        if failure == "DOCKER_DOWN":
            assert "the Docker daemon did not answer" in result.stderr.decode()
        if failure != "FAIL_POLICY":
            assert "docker compose stop scrapeyard" not in calls
        else:
            assert "the application is left stopped" in result.stderr.decode()
    # Docker starts a container unconfined, under the profile's name, where
    # it cannot use AppArmor. Only the kernel's label says so, after the start.
    for label in ("unconfined", "docker-default (enforce)", "scrapeyard-chromium (complain)", ""):
        log.write_text("")
        result = subprocess.run(
            command, cwd=tmp_path, env=dict(env, APP_LABEL=label), capture_output=True
        )
        assert result.returncode == 2, label
        assert "not under the scrapeyard-chromium AppArmor profile" in result.stderr.decode()
        assert "the application is left stopped" in result.stderr.decode()
        calls = log.read_text().splitlines()
        started = calls.index("docker compose up -d --wait --no-build --pull never")
        assert "docker compose stop scrapeyard" in calls[started:]


# A start that succeeded ends with the kernel being asked what confines it.
_STARTED_AND_JUDGED = [
    "docker compose up -d --wait --no-build --pull never",
    "docker compose exec -T scrapeyard cat /proc/1/attr/current",
]


def _wrapper_with_fake_docker(tmp_path, docker_script):
    security = tmp_path / "security"
    security.mkdir()
    wrapper = security / "deploy-secure-compose.sh"
    text = Path("security/deploy-secure-compose.sh").read_text()
    for old, new in (
        ("((EUID == 0)) || fail", "true || fail"),
        ("LOCK_DIR=/run/scrapeyard", f"LOCK_DIR={tmp_path / 'lock'}"),
        ("flock --wait 600 9", "flock --nonblock 9"),
        ("    sleep 1\n", "    sleep 0\n"),
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


# APP_STATE is what Docker reports for the application's container after a
# stop ("unreadable" fails the read). The container exists from the start with
# APP_EXISTS=yes, and otherwise once the application has been started.
# REMOVAL=fails makes every `docker rm` fail, and APP_ONEOFF=True makes the
# container one that `docker compose run` created. APP_POLICY is its restart
# policy ("unreadable" fails the read).
_STATE = '{{index .Config.Labels "com.docker.compose.oneoff"}} {{.State.Status}}'
_FAKE_DOCKER = """#!/bin/bash
echo "docker $*" >> "$CALLS"
present() { [[ ! -e "$CALLS.removed" && ( "${APP_EXISTS:-}" == yes || -e "$CALLS.started" ) ]]; }
case "$*" in
  "info --format "*) [[ "${DOCKER_DOWN:-}" != yes ]] || { echo "[]"; exit 1; }
    [[ "${NO_APPARMOR:-}" == yes ]] || echo "[name=apparmor,profile=default name=seccomp,profile=builtin]" ;;
  "compose exec -T scrapeyard cat /proc/1/attr/current") echo "${APP_LABEL-scrapeyard-chromium (enforce)}" ;;
  "compose config --images") echo sha256:aaaaaaaa ;;
  "compose ps -q egress-probe") echo probe ;;
  "compose stop scrapeyard")
    if [[ -e "$CALLS.started" && "${SIGNAL_DURING_CLEANUP:-}" == yes ]]; then kill -TERM "$PPID"; fi ;;
  "compose ps --all --quiet scrapeyard") [[ "${DOCKER_PS:-}" != fails ]] || exit 1; ! present || echo app1 ;;
  "ps --all --quiet --filter id=app1") ! present || echo app1 ;;
  'inspect --format {{index .Config.Labels "com.docker.compose.oneoff"}} {{.State.Status}} app1')
    [[ "${APP_STATE:-}" != unreadable ]] || exit 1; echo "${APP_ONEOFF:-False} ${APP_STATE:-exited}" ;;
  "inspect --format {{.HostConfig.RestartPolicy.Name}} app1")
    [[ "${APP_POLICY:-}" != unreadable ]] || exit 1; echo "${APP_POLICY:-unless-stopped}" ;;
  "logs --tail 50 app1") echo "ValidationError: input_value='c2VjcmV0'" ;;
  "rm --force app1") [[ "${REMOVAL:-works}" == works ]] || exit 1; touch "$CALLS.removed" ;;
  "inspect "*) echo abcdef123456 ;;
  "network inspect "*) echo br-release ;;
  "compose up -d --wait --no-build --pull never") touch "$CALLS.started"; exit "${START_STATUS:-0}" ;;
esac
"""
_CLEARING = [
    "docker compose stop scrapeyard",
    "docker compose ps --all --quiet scrapeyard",
    f"docker inspect --format {_STATE} app1",
    "docker logs --tail 50 app1",
    "docker inspect --format {{.HostConfig.RestartPolicy.Name}} app1",
    "docker update --restart=no app1",
    "docker rm --force app1",
    "docker ps --all --quiet --filter id=app1",
]


def test_a_start_that_fails_leaves_the_application_stopped_not_restarting(tmp_path):
    command, env = _wrapper_with_fake_docker(tmp_path, _FAKE_DOCKER)

    result = subprocess.run(command, cwd=tmp_path, env=dict(env, START_STATUS="1"), capture_output=True)

    assert result.returncode == 1
    calls = Path(env["CALLS"]).read_text().splitlines()
    # The container is unless-stopped: left alone it would keep restarting.
    assert calls[-4:] == ["docker compose up -d --wait --no-build --pull never", *_CLEARING[:3]]
    assert b"the application is left stopped" in result.stderr

    Path(env["CALLS"]).write_text("")
    subprocess.run(command, cwd=tmp_path, env=env, check=True, capture_output=True)
    assert Path(env["CALLS"]).read_text().splitlines()[-2:] == _STARTED_AND_JUDGED


@pytest.mark.parametrize("state", ["restarting", "created", "running", "paused", "unreadable"])
def test_a_container_docker_cannot_stop_is_removed_after_a_failed_start(tmp_path, state):
    # runc 1.3.3 could not start a container under the profile of 2026-10-10.
    # Docker restarted it without end and `docker stop` reported "did not
    # receive an exit event", so the stop alone left it restarting.
    command, env = _wrapper_with_fake_docker(tmp_path, _FAKE_DOCKER)
    # Output kept from an earlier removal, readable by others, is replaced.
    kept = tmp_path / "lock" / "removed-container.log"
    kept.parent.mkdir()
    kept.write_text("an earlier container\n")
    kept.chmod(0o644)

    result = subprocess.run(
        command, cwd=tmp_path, env=dict(env, START_STATUS="1", APP_STATE=state), capture_output=True
    )

    assert result.returncode == 1
    assert Path(env["CALLS"]).read_text().splitlines()[-len(_CLEARING) :] == _CLEARING
    stderr = result.stderr.decode()
    shown = "unknown" if state == "unreadable" else state
    assert f"the application's container is {shown}, not stopped, and is being removed" in stderr
    assert f"its last output is in {kept}" in stderr
    assert stderr.rstrip().endswith("failed; the application is left stopped")
    # A start that fails on a setting prints part of it; the journal gets none.
    assert kept.read_text() == "ValidationError: input_value='c2VjcmV0'\n"
    assert kept.stat().st_mode & 0o777 == 0o600
    assert "c2VjcmV0" not in stderr and b"c2VjcmV0" not in result.stdout


def test_a_failed_start_says_so_when_its_container_cannot_be_removed(tmp_path):
    command, env = _wrapper_with_fake_docker(tmp_path, _FAKE_DOCKER)

    result = subprocess.run(
        command,
        cwd=tmp_path,
        env=dict(env, START_STATUS="1", APP_STATE="restarting", REMOVAL="fails"),
        capture_output=True,
    )

    assert result.returncode == 1
    calls = Path(env["CALLS"]).read_text().splitlines()
    assert calls.count("docker rm --force app1") == 3
    # It keeps the policy it had: a later start must not find it without one.
    assert calls[-1] == "docker update --restart=unless-stopped app1"
    stderr = result.stderr.decode()
    assert "the application is left stopped" not in stderr
    assert "could not be stopped or removed; see 'docker ps --all'" in stderr


@pytest.mark.parametrize(
    ("policy", "updates"), [("no", ["docker update --restart=no app1"]), ("", []), ("unreadable", [])]
)
def test_a_container_that_cannot_be_removed_is_not_given_a_policy_it_did_not_have(tmp_path, policy, updates):
    command, env = _wrapper_with_fake_docker(tmp_path, _FAKE_DOCKER)
    fake = Path(env["PATH"].split(":")[0]) / "docker"
    # An empty answer cannot be asked for through the environment default.
    fake.write_text(fake.read_text().replace('echo "${APP_POLICY:-unless-stopped}"', 'echo "${APP_POLICY-unless-stopped}"'))

    result = subprocess.run(
        command,
        cwd=tmp_path,
        env=dict(env, START_STATUS="1", APP_STATE="restarting", REMOVAL="fails", APP_POLICY=policy),
        capture_output=True,
    )

    assert result.returncode == 1
    calls = Path(env["CALLS"]).read_text().splitlines()
    # Unread, the policy is left as it is: switched off, it could not be put back.
    assert [call for call in calls if call.startswith("docker update")] == updates
    assert calls.count("docker rm --force app1") == 3
    assert b"could not be stopped or removed" in result.stderr


def test_a_signal_while_a_failed_start_is_cleared_does_not_cut_it_short(tmp_path):
    command, env = _wrapper_with_fake_docker(tmp_path, _FAKE_DOCKER)

    result = subprocess.run(
        command,
        cwd=tmp_path,
        env=dict(env, START_STATUS="1", APP_STATE="restarting", SIGNAL_DURING_CLEANUP="yes"),
        capture_output=True,
    )

    # The start's own failure is reported, not the signal's 143.
    assert result.returncode == 1
    assert Path(env["CALLS"]).read_text().splitlines()[-len(_CLEARING) :] == _CLEARING
    assert result.stderr.decode().rstrip().endswith("failed; the application is left stopped")


def test_a_container_the_operator_ran_by_hand_is_left_alone(tmp_path):
    command, env = _wrapper_with_fake_docker(tmp_path, _FAKE_DOCKER)

    result = subprocess.run(
        command,
        cwd=tmp_path,
        env=dict(env, START_STATUS="1", APP_STATE="running", APP_ONEOFF="True"),
        capture_output=True,
    )

    assert result.returncode == 1
    calls = Path(env["CALLS"]).read_text().splitlines()
    assert calls[-1] == f"docker inspect --format {_STATE} app1"
    assert not any(call.startswith(("docker rm", "docker update", "docker logs")) for call in calls)


@pytest.mark.parametrize(
    ("fault", "removals"),
    [({"APP_STATE": "restarting", "REMOVAL": "fails"}, 3), ({"DOCKER_PS": "fails"}, 0)],
)
def test_a_deployment_is_refused_while_an_earlier_container_cannot_be_cleared(tmp_path, fault, removals):
    command, env = _wrapper_with_fake_docker(tmp_path, _FAKE_DOCKER)

    result = subprocess.run(command, cwd=tmp_path, env=dict(env, APP_EXISTS="yes", **fault), capture_output=True)

    assert result.returncode == 2
    calls = Path(env["CALLS"]).read_text().splitlines()
    assert calls.count("docker rm --force app1") == removals
    assert not any(call.startswith("docker compose up") for call in calls)
    assert b"the application's container could not be stopped or removed" in result.stderr


def test_a_deployment_clears_a_container_that_never_ran_and_keeps_one_that_stopped(tmp_path):
    command, env = _wrapper_with_fake_docker(tmp_path, _FAKE_DOCKER)

    subprocess.run(
        command, cwd=tmp_path, env=dict(env, APP_EXISTS="yes", APP_STATE="created"), check=True, capture_output=True
    )

    calls = Path(env["CALLS"]).read_text().splitlines()
    assert calls.index("docker rm --force app1") < calls.index(
        "docker compose up -d --wait --no-build --pull never egress-probe redis"
    )

    Path(env["CALLS"]).write_text("")
    Path(env["CALLS"] + ".removed").unlink()
    subprocess.run(command, cwd=tmp_path, env=dict(env, APP_EXISTS="yes"), check=True, capture_output=True)
    calls = Path(env["CALLS"]).read_text().splitlines()
    assert f"docker inspect --format {_STATE} app1" in calls
    assert not any(call.startswith(("docker rm", "docker update")) for call in calls)


def test_a_deployment_needs_a_proxy_unless_submissions_are_first_party(tmp_path):
    command, env = _wrapper_with_fake_docker(tmp_path, _FAKE_DOCKER)
    calls = Path(env["CALLS"])
    env.pop("SCRAPEYARD_PROXY_URL")

    # Compose's default is untrusted submissions, which need the proxy.
    for untrusted in (None, "true", "0", "False", "no"):
        calls.write_text("")
        mode = {} if untrusted is None else {"SCRAPEYARD_UNTRUSTED_SUBMISSIONS": untrusted}
        result = subprocess.run(command, cwd=tmp_path, env=dict(env, **mode), capture_output=True)
        assert result.returncode == 2, untrusted
        assert b"must name an egress-filtering operator proxy" in result.stderr
        # Refused before the release is loaded or anything is stopped.
        assert calls.read_text().splitlines() == ["docker compose version"]
    refused = subprocess.run(
        command,
        cwd=tmp_path,
        env=dict(env, SCRAPEYARD_UNTRUSTED_SUBMISSIONS="true", SCRAPEYARD_PROXY_URL="direct"),
        capture_output=True,
    )
    assert refused.returncode == 2

    calls.write_text("")
    subprocess.run(
        command,
        cwd=tmp_path,
        env=dict(env, SCRAPEYARD_UNTRUSTED_SUBMISSIONS="false"),
        check=True,
        capture_output=True,
    )
    assert calls.read_text().splitlines()[-2:] == _STARTED_AND_JUDGED


@pytest.mark.parametrize(
    ("setting", "values"),
    [
        # Any spelling pydantic reads as zero, a blank host, or simply another value.
        ("SCRAPEYARD_EGRESS_POLICY_PROBE_PORT", ["0", "+0", "0.0", " 0", "0_0", "8090"]),
        ("SCRAPEYARD_EGRESS_POLICY_PROBE_LIVENESS_PORT", ["0", "-0", "8091"]),
        ("SCRAPEYARD_EGRESS_POLICY_PROBE_HOST", [" ", "127.0.0.1", "0.0.0.0"]),
        # A challenge that times out counts as refused, so the timeout must
        # leave a connection time to complete.
        ("SCRAPEYARD_EGRESS_POLICY_PROBE_TIMEOUT_SECONDS", ["0.0005", "0.5", "0", "1e-9", "soon", " 1"]),
    ],
)
@pytest.mark.parametrize("mode", ["true", "false"])
def test_a_deployment_cannot_weaken_the_startup_check_of_the_policy(tmp_path, setting, values, mode):
    command, env = _wrapper_with_fake_docker(tmp_path, _FAKE_DOCKER)
    env = dict(env, SCRAPEYARD_UNTRUSTED_SUBMISSIONS=mode)
    calls = Path(env["CALLS"])

    for value in values:
        calls.write_text("")
        result = subprocess.run(command, cwd=tmp_path, env=dict(env, **{setting: value}), capture_output=True)
        assert result.returncode == 2, value
        assert setting.encode() in result.stderr
        # Refused before the release is loaded or anything is stopped.
        assert calls.read_text().splitlines() == ["docker compose version"]
    # Unset or empty leaves the Compose file's value in place.
    subprocess.run(command, cwd=tmp_path, env=dict(env, **{setting: ""}), check=True, capture_output=True)


def test_a_deployment_may_lengthen_the_probe_timeout(tmp_path):
    command, env = _wrapper_with_fake_docker(tmp_path, _FAKE_DOCKER)
    for value in ("1", "2", "2.5", "10"):
        subprocess.run(
            command,
            cwd=tmp_path,
            env=dict(env, SCRAPEYARD_EGRESS_POLICY_PROBE_TIMEOUT_SECONDS=value),
            check=True,
            capture_output=True,
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
