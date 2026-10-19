from __future__ import annotations

import configparser
import fcntl
import hashlib
import io
import ipaddress
import json
import os
import re
import signal
import subprocess
import sys
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

SCRIPT = Path("security/backup.sh")
MANAGE = Path("security/manage-release.sh")
SERVICE = Path("security/systemd/scrapeyard-backup.service")
TIMER = Path("security/systemd/scrapeyard-backup.timer")
ARCHIVE = "scrapeyard-backup-????????T??????Z.tar.zst.age"
ISOLATED = "scrapeyard-restore-test"
RECIPIENT = "age1" + "q" * 58
WITNESS = {
    "format": "scrapeyard-backup-witness-v1",
    "projects": ["td21"],
    "jobs": [{"job_id": "j1", "status": "complete"}, {"job_id": "j2", "status": "failed"}],
    "samples": {
        "complete": {"job_id": "j1", "detail_sha256": "aa", "config_sha256": "bb"},
        "failed": {"job_id": "j2", "detail_sha256": "cc", "config_sha256": "dd"},
    },
}

# Docker as the script meets it: one application and one Redis container of
# the deployment, and whatever the restore test creates. It remembers what
# exists in files, records every call, and marks any removal that reaches
# the deployment.
DOCKER = r'''#!/usr/bin/env python3
import fcntl
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

args = sys.argv[1:]
env = os.environ
state = Path(env["STATE"])
DEPLOYMENT, ISOLATED = "scrapeyard", "scrapeyard-restore-test"


def record(line):
    with open(env["CALLS"], "a") as handle:
        handle.write(line + "\n")


def flag(name):
    return (state / name).exists()


def label(kind):
    prefix = f"label=com.docker.compose.{kind}="
    return next((arg[len(prefix):] for arg in args if arg.startswith(prefix)), None)


record("docker " + " ".join(args))
command = args[0]
if command == "ps":
    if env.get("DOCKER_PS_FAILS") == "yes":
        sys.exit(1)
    # A daemon that stops answering once the application has been stopped.
    if env.get("PS_FAILS_ONCE_STOPPED") == "yes" and flag("stop-asked") and "-aq" not in args:
        sys.exit(1)
    # A daemon that stops answering once the restore test's copy has started.
    if env.get("LISTING_FAILS_ONCE_STARTED") == "yes" and flag("copy-was-started") and "-aq" in args:
        sys.exit(1)
    deployed = env.get("NO_DEPLOYMENT") != "yes"
    containers = {
        "a11": (DEPLOYMENT, "scrapeyard", deployed and (flag("app-running") or "-aq" in args)),
        "b22": (DEPLOYMENT, "redis", deployed),
        "c33": (ISOLATED, "scrapeyard", flag("isolated-app")),
        "d44": (ISOLATED, "redis", flag("isolated-redis")),
    }
    wanted = next((arg[3:] for arg in args if arg.startswith("id=")), None)
    for container, (project, service, listed) in containers.items():
        if (
            listed
            and wanted in (None, container)
            and label("project") in (None, project)
            and label("service") in (None, service)
        ):
            print(container)
elif command == "inspect":
    template = args[2]
    if ".Mounts" in template:
        print(env.get("DATA_MOUNT", "volume:scrapeyard_scrapeyard-data"))
    else:
        print(env.get("RUNNING_IMAGE", "sha256:new"))
elif command == "info":
    print(state)
elif command == "image":
    if env.get("IMAGES_MISSING") == "yes":
        sys.exit(1)
    if "--format" in args:
        print(f"{os.getuid()}:{os.getgid()}")
elif command == "exec":
    rest = [arg for arg in args[1:] if arg != "-i"]
    container, program = rest[0], rest[1]
    if program == "redis-cli":
        question = rest[2].upper()
        # The restored Redis holds what it loaded: one key had expired.
        keys = env.get("RESTORED_KEYS", "4") if container == "d44" else "5"
        database = env.get("REDIS_DB", "0")
        if question == "DBSIZE":
            # The database a connection starts in, the first.
            print(keys if database == "0" else "0")
        elif question == "INFO" and rest[3:] == ["keyspace"]:
            # Redis lists only the databases that hold keys.
            print("# Keyspace\r")
            if keys != "0":
                print(f"db{database}:keys={keys},expires=1,avg_ttl=0,subexpiry=0\r")
        elif question == "SAVE":
            print("ERR Background save already in progress" if env.get("SAVE_FAILS") == "yes" else "OK")
        else:
            print("aof_enabled:1\r")
    elif program == "cat":
        # What the kernel reports for the container's first process.
        print(env.get("KERNEL_LABEL", "scrapeyard-chromium (enforce)"))
    else:
        sys.stdin.read()
        question = rest[3]
        if question == "witness" and container == "a11":
            busy = state / "busy"
            left = int(busy.read_text()) if busy.exists() else 0
            if left > 0:
                busy.write_text(str(left - 1))
                print("not idle: status ok, 1 running, 0 queued", file=sys.stderr)
                sys.exit(3)
            if env.get("CANNOT_ASK") == "yes":
                print("no credential in SCRAPEYARD_API_CREDENTIALS holds the health-detail scope", file=sys.stderr)
                sys.exit(2)
            if env.get("ASK_FAILS") == "yes":
                # The answer that is neither "idle" nor "not now".
                print("a result read answered HTTP 429", file=sys.stderr)
                sys.exit(4)
            if env.get("UNHEALTHY") == "yes":
                print("unhealthy: /health/ready answered HTTP 503", file=sys.stderr)
                sys.exit(3)
            sys.stdout.write((state / "witness.json").read_text())
        elif question == "restored":
            (state / "left-out").write_text(" ".join(rest[4:]))
            name = "restored.json" if flag("restored.json") else "witness.json"
            sys.stdout.write((state / name).read_text())
        elif question == "envelope":
            print("test-key")
        elif question == "no-route":
            sys.exit(4 if env.get("HAS_ROUTE") == "yes" else 0)
elif command == "cp":
    if env.get("DUMP_COPY_FAILS") == "yes":
        sys.exit(1)
    Path(args[2]).write_bytes(b"REDIS-DUMP")
elif command == "run":
    mounts = {}
    for arg in args:
        if arg.startswith("type="):
            fields = dict(item.split("=", 1) for item in arg.split(",") if "=" in item)
            mounts[fields["target"]] = (fields["source"], arg.endswith(",readonly"))
    if "du" in args:
        # The measurement of the data, taken while the application runs.
        record(
            f"measured: application {'running' if flag('app-running') else 'stopped'}, "
            f"data volume {'read-only' if mounts['/source'][1] else 'WRITABLE'}, "
            f"network {args[args.index('--network') + 1]}"
        )
        print(f"{env.get('DATA_MB', '1')}\t/source/db\n0\t/source/results\n0\t/source/adaptive")
        sys.exit(0)
    # The helper's command is one argument to the container's shell.
    if any(" create " in arg for arg in args):
        with open(Path(env["LOCK"]) / "release.lock", "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = "free"
            except BlockingIOError:
                held = "held"
        record(
            f"copy: release lock {held}, "
            f"application {'running' if flag('app-running') else 'stopped'}, "
            f"data volume {mounts['/source'][0]} {'read-only' if mounts['/source'][1] else 'WRITABLE'}, "
            f"network {args[args.index('--network') + 1]}"
        )
        if env.get("COPY_HANGS") == "yes":
            (state / "copy-started").touch()
            time.sleep(120)
        if env.get("COPY_FAILS") == "yes":
            sys.exit(1)
        made = Path(mounts["/backup"][0]) / "set"
        (made / "payload" / "db").mkdir(parents=True)
        # The copied database: the witnessed jobs, and any that the
        # application accepted between its last answer and the stop.
        stored = [(job["job_id"], "td21") for job in json.loads((state / "witness.json").read_text())["jobs"]]
        stored += [tuple(job.split("=")) for job in env.get("ACCEPTED_SINCE", "").split()]
        database = sqlite3.connect(made / "payload" / "db" / "jobs.db")
        # As SQLite's backup API leaves it: in WAL mode, with no sidecar file
        # until something opens it for writing.
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("CREATE TABLE jobs (job_id TEXT, project TEXT)")
        database.executemany("INSERT INTO jobs VALUES (?, ?)", stored)
        database.commit()
        database.close()
        (made / "manifest.json").write_text('{"format": "scrapeyard-backup-v1"}\n')
        Path(mounts["/data"][0], "copied").write_text("PLAINTEXT-OF-THE-DATABASE")
    elif env.get("SET_INVALID") == "yes":
        sys.exit(1)
    print("{}")
elif command == "compose":
    if args[1] == "version":
        sys.exit(0)
    named = args.index("--project-name")
    project = args[named + 1]
    record(
        f"compose for {project}: files {env.get('COMPOSE_FILE')}, "
        f"project variable {env.get('COMPOSE_PROJECT_NAME', 'unset')}, in {os.getcwd()}"
    )
    action = args[named + 2]
    if action == "run" and "restore" in args:
        (state / ("isolated-volumes" if project == ISOLATED else "deployment-volumes")).touch()
        print("{}")
    elif action == "run":
        print(env.get("REDIS_LOADS", "4 1"))
    elif action == "up":
        isolated = [name for name in env["COMPOSE_FILE"].split(":") if name.endswith("isolated.yml")]
        if isolated:
            (state / "isolated.yml").write_text(Path(isolated[0]).read_text())
        (state / ("isolated-app" if args[-1] == "scrapeyard" else "isolated-redis")).touch()
        if args[-1] == "scrapeyard":
            (state / "copy-was-started").touch()
elif command == "network":
    action = args[1]
    if action == "create":
        (state / "network").write_text("internal" if "--internal" in args else "open")
    elif action == "inspect":
        if not flag("network"):
            sys.exit(1)
        internal = (state / "network").read_text() == "internal"
        print(env.get("NETWORK_REPORTS", "true" if internal else "false"), "ab" * 32)
    elif action == "ls":
        if env.get("LISTING_FAILS_ONCE_STARTED") == "yes" and flag("copy-was-started"):
            sys.exit(1)
        if flag("network") and label("project") == ISOLATED:
            print("e55")
    elif action == "rm" and env.get("NETWORK_STAYS") != "yes":
        (state / "network").unlink(missing_ok=True)
elif command == "volume":
    action = args[1]
    if action == "ls":
        if label("project") == ISOLATED and flag("isolated-volumes"):
            print("v66")
        deployed = env.get("NO_DEPLOYMENT") != "yes" or flag("deployment-volumes")
        if label("project") == DEPLOYMENT and deployed:
            print("scrapeyard_scrapeyard-data")
    elif action == "rm":
        if args[2:] != ["v66"]:
            record("VIOLATION: a volume of the deployment was removed")
        (state / "isolated-volumes").unlink(missing_ok=True)
elif command == "rm":
    if {"a11", "b22"} & set(args):
        record("VIOLATION: a container of the deployment was removed")
    for container, name in (("c33", "isolated-app"), ("d44", "isolated-redis")):
        if container in args:
            (state / name).unlink(missing_ok=True)
elif command == "logs":
    print("the application's last output")
'''

# The unit as the script meets it. Its start takes the release lock, as
# scrapeyard-release does, so a start asked for while the script still holds
# the lock fails here and would wait five minutes on a host.
SYSTEMCTL = r'''#!/bin/bash
echo "systemctl $*" >> "$CALLS"
case "$*" in
  "is-active scrapeyard-promote.service")
    state=$(cat "$STATE/promotion" 2>/dev/null || echo inactive)
    echo "$state"; [[ "$state" == active ]] ;;
  "is-active scrapeyard.service")
    if [[ -e "$STATE/unit-state" ]]; then cat "$STATE/unit-state"; exit 3; fi
    if [[ -e "$STATE/unit-active" ]]; then echo active; else echo inactive; exit 3; fi ;;
  "stop scrapeyard.service")
    touch "$STATE/stop-asked"
    [[ "${STOP_FAILS:-}" != yes ]] || exit 1
    # systemctl reports success for a stop whose ExecStop failed.
    [[ "${STOP_LEAVES_IT_RUNNING:-}" != yes ]] || exit 0
    rm -f "$STATE/unit-active" "$STATE/app-running" ;;
  "start scrapeyard.service"|"start --no-block scrapeyard.service")
    if ! flock --nonblock "$LOCK/release.lock" true; then
      echo "systemctl start: the release lock is still held" >> "$CALLS"
      exit 1
    fi
    [[ "${START_FAILS:-}" != yes ]] || exit 1
    touch "$STATE/unit-active" "$STATE/app-running" ;;
esac
'''

# age without the cryptography: one line in front of what it is given. Like
# age, it refuses a recipients file with a line that is not a recipient.
AGE = r'''#!/bin/bash
echo "age $*" >> "$CALLS"
if [[ "$1" == --encrypt ]]; then
  number=0 found=0
  while IFS= read -r line || [[ -n "$line" ]]; do
    number=$((number + 1))
    [[ -z "$line" || "$line" == \#* ]] && continue
    if [[ "$line" =~ ^age1[a-z0-9]{58}$ ]]; then found=1; continue; fi
    echo "age: error: malformed recipient at line $number" >&2
    cat > /dev/null
    exit 1
  done < "$3"
  ((found == 1)) || { echo "age: error: no recipients found" >&2; cat > /dev/null; exit 1; }
  { echo age-stand-in; cat; } > "$5"
else
  tail -n +2 "$4"
fi
'''

# The host's bridge for the restore test's network, as the kernel shows it.
IP = r'''#!/bin/bash
echo "ip $*" >> "$CALLS"
[[ "${BRIDGE_IS_MISSING:-}" != yes ]] || { echo "Device does not exist" >&2; exit 1; }
[[ "${BRIDGE_HAS_AN_ADDRESS:-}" != yes ]] || echo "7: ${*: -1}    inet 172.30.1.1/24 brd 172.30.1.255 scope global"
'''

# df, except where a test says how much room Docker's own directory has.
DF = r'''#!/bin/bash
if [[ -n "${DOCKER_ROOT_FREE_MB:-}" && "${*: -1}" == "$STATE" ]]; then
  printf 'Avail\n%sM\n' "$DOCKER_ROOT_FREE_MB"
else
  exec /usr/bin/df "$@"
fi
'''


def _mode(path: Path, mode: int) -> Path:
    path.chmod(mode)
    return path


class _Host:
    """A copy of the script with its fixed paths moved, and stand-ins for the host's tools."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = _mode(tmp_path, 0o755)
        self.calls = tmp_path / "calls"
        self.state = tmp_path / "state"
        self.etc = tmp_path / "etc"
        self.lock_dir = tmp_path / "run"
        self.releases = tmp_path / "releases"
        self.backups = tmp_path / "backups"
        self.status = tmp_path / "lib" / "backup-status.json"
        self.env_file = self.etc / "deploy.env"
        self.recipients = self.etc / "backup-recipients"
        self.hook = self.etc / "backup-upload"
        self.identity = tmp_path / "identity"
        self.current = self.releases / "current"
        self.marker = self.lock_dir / "backup-stopped-application"
        self.script = tmp_path / "backup.sh"
        text = SCRIPT.read_text(encoding="utf-8")
        for old, new in (
            ('((EUID == 0)) || fail "run as root"', 'true || fail "run as root"'),
            ("ENV_FILE=/etc/scrapeyard/deploy.env", f"ENV_FILE={self.env_file}"),
            ("CURRENT=/srv/scrapeyard-releases/current", f"CURRENT={self.current}"),
            ("LOCK_DIR=/run/scrapeyard", f"LOCK_DIR={self.lock_dir}"),
            ("RECIPIENTS=/etc/scrapeyard/backup-recipients", f"RECIPIENTS={self.recipients}"),
            ("UPLOAD_HOOK=/etc/scrapeyard/backup-upload", f"UPLOAD_HOOK={self.hook}"),
            ("BACKUP_DIR=/var/backups/scrapeyard", f"BACKUP_DIR={self.backups}"),
            ("STATUS_FILE=/var/lib/scrapeyard/backup-status.json", f"STATUS_FILE={self.status}"),
            ("TRUSTED_OWNER=0", f"TRUSTED_OWNER={os.getuid()}"),
            ("TRUSTED_ROOT=/", f"TRUSTED_ROOT={tmp_path}"),
        ):
            assert text.count(old) == 1, old
            text = text.replace(old, new)
        self.script.write_text(text, encoding="utf-8")
        self.script.chmod(0o755)
        # Modes are set here, whatever umask the tests run under: the script
        # refuses what a group could write.
        for directory in (self.state, self.etc, self.releases, tmp_path / "bin"):
            directory.mkdir()
            directory.chmod(0o755)
        self.lock_dir.mkdir(mode=0o700)
        self.env_file.write_text(
            "COMPOSE_PROJECT_NAME='scrapeyard'\n"
            "SCRAPEYARD_API_CREDENTIALS='hunter2-credential'\n"
        )
        self.env_file.chmod(0o600)
        self.recipients.write_text(f"# the operator's key\n{RECIPIENT}\n")
        self.recipients.chmod(0o644)
        self.identity.write_text("AGE-SECRET-KEY-1TEST\n")
        self.identity.chmod(0o600)
        self.release = self._release("scrapeyard-release-new")
        self.current.symlink_to(self.release)
        (self.state / "witness.json").write_text(json.dumps(WITNESS, indent=1, sort_keys=True) + "\n")
        (self.state / "unit-active").touch()
        (self.state / "app-running").touch()
        self.bin = tmp_path / "bin"
        self.tool("docker", DOCKER)
        self.tool("systemctl", SYSTEMCTL)
        self.tool("age", AGE)
        self.tool("ip", IP)
        self.tool("df", DF)
        # tar asks a program named zstd to compress; the runner has it, a
        # workstation may not, and the tests do not depend on it.
        self.tool("zstd", "#!/bin/bash\nexec cat\n")
        # The script's own pauses are not what is tested.
        self.tool("sleep", "#!/bin/bash\nexit 0\n")
        self.env = dict(
            os.environ,
            PATH=f"{self.bin}:{os.environ['PATH']}",
            CALLS=str(self.calls),
            STATE=str(self.state),
            LOCK=str(self.lock_dir),
        )

    def _release(self, name: str) -> Path:
        release = self.releases / name
        for directory in ("source/security", "source/scripts", "source/sql"):
            (release / directory).mkdir(parents=True)
        (release / "manifest.json").write_text(
            json.dumps({"images": {"scrapeyard": "sha256:new", "redis": "sha256:redis"}})
        )
        (release / "images.json").write_text("{}")
        (release / "source" / "docker-compose.yml").write_text("services: {}\n")
        (release / "source" / "scripts" / "qualification_backup.py").write_text("")
        for script, line in (
            ("security/deploy-secure-compose.sh", "exit 0"),
            ("security/install-chromium-apparmor-profile.sh", 'echo "apparmor $*" >> "$CALLS"'),
        ):
            (release / "source" / script).write_text(f"#!/bin/bash\n{line}\n")
        (release / "source" / "scripts" / "release_artifact.py").write_text(
            "import os, sys\n"
            "open(os.environ['CALLS'], 'a').write('release_artifact ' + ' '.join(sys.argv[1:]) + '\\n')\n"
        )
        for path in [release, *release.rglob("*")]:
            path.chmod(0o755 if path.is_dir() or path.suffix == ".sh" else 0o644)
        return release

    def tool(self, name: str, text: str) -> None:
        path = self.bin / name
        path.write_text(text)
        path.chmod(0o755)

    def install_hook(self, body: str = 'echo "$1" > "$STATE/uploaded"\n') -> None:
        self.hook.write_text("#!/bin/bash\n" + body)
        self.hook.chmod(0o755)

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.script), *args],
            env=dict(self.env, **env),
            capture_output=True,
            text=True,
            timeout=60,
        )

    def called(self, prefix: str = "") -> list[str]:
        lines = self.calls.read_text().splitlines() if self.calls.exists() else []
        assert not [line for line in lines if line.startswith("VIOLATION")], lines
        return [line for line in lines if line.startswith(prefix)]

    def first(self, fragment: str) -> int:
        return next(i for i, call in enumerate(self.called()) if fragment in call)

    def archives(self) -> list[Path]:
        return sorted(
            path for path in self.backups.glob(ARCHIVE) if path.is_file() and not path.is_symlink()
        )

    def left_in_backups(self) -> set[str]:
        return {path.name for path in self.backups.iterdir()} if self.backups.exists() else set()

    def application_is_running(self) -> bool:
        return (self.state / "app-running").exists() and (self.state / "unit-active").exists()

    def release_lock_is_free(self) -> bool:
        with (self.lock_dir / "release.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
        return True

    def stopped(self) -> bool:
        return bool(self.called("systemctl stop"))

    def encrypted(self) -> list[str]:
        # Every backup asks age to judge the recipients file, writing nothing.
        return [call for call in self.called("age --encrypt") if not call.endswith("--output /dev/null")]

    def members(self, archive: Path) -> dict[str, bytes]:
        header, _, packed = archive.read_bytes().partition(b"\n")
        assert header == b"age-stand-in"
        with tarfile.open(fileobj=io.BytesIO(packed)) as tar:
            return {
                member.name.removeprefix("./"): tar.extractfile(member).read()  # type: ignore[union-attr]
                for member in tar.getmembers()
                if member.isfile()
            }


@pytest.fixture
def host(tmp_path: Path) -> _Host:
    return _Host(tmp_path)


def test_backup_copies_while_stopped_and_encrypts_only_after_the_application_is_back(host):
    result = host.run("run")

    assert result.returncode == 0, result.stderr
    # Asked while it runs, copied while it is stopped, encrypted once it is back.
    steps = [
        host.first(fragment)
        for fragment in (
            "docker exec -i a11 python - witness",
            "systemctl stop scrapeyard.service",
            "docker exec b22 redis-cli SAVE",
            "docker cp b22:/data/dump.rdb",
            "copy: ",
            "systemctl start scrapeyard.service",
            "docker exec -i a11 python - ready",
            f"--output {host.backups}",
        )
    ]
    assert steps == sorted(steps)
    # What can be refused is asked before the stop: age judges the recipients,
    # and the data is measured, read-only and without a network, while the
    # application still runs.
    assert host.first("--output /dev/null") < host.first("measured: ") < host.first("systemctl stop")
    assert host.called("measured: ") == [
        "measured: application running, data volume read-only, network none"
    ]
    # No start or promotion can begin between "idle" and the end of the copy,
    # and the copy cannot write to the deployment's data or reach a network.
    assert host.called("copy: ") == [
        "copy: release lock held, application stopped, "
        "data volume scrapeyard_scrapeyard-data read-only, network none"
    ]
    assert host.application_is_running() and host.release_lock_is_free()
    assert not host.marker.exists()
    assert all(f"--recipients-file {host.recipients}" in call for call in host.called("age"))
    assert len(host.encrypted()) == 1

    (archive,) = host.archives()
    assert host.left_in_backups() == {archive.name, archive.name + ".sha256"}
    assert archive.stat().st_mode & 0o077 == 0 and host.backups.stat().st_mode & 0o077 == 0
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert (host.backups / f"{archive.name}.sha256").read_text() == f"{digest}  {archive.name}\n"
    members = host.members(archive)
    assert set(members) == {
        "metadata.json",
        "witness.json",
        "redis/dump.rdb",
        "set/manifest.json",
        "set/payload/db/jobs.db",
    }
    assert json.loads(members["witness.json"]) == WITNESS
    metadata = json.loads(members["metadata.json"])
    assert metadata["format"] == "scrapeyard-host-backup-v1"
    assert metadata["release"] == "scrapeyard-release-new"
    assert metadata["redis_keys"] == 5
    assert metadata["accepted_after_witness"] == []
    assert metadata["manifest_sha256"] == hashlib.sha256(members["set/manifest.json"]).hexdigest()
    assert archive.name == "scrapeyard-backup-" + re.sub(r"[-:]", "", metadata["created_at"]) + ".tar.zst.age"

    status = json.loads(host.status.read_text())
    assert status["archive"] == str(archive) and status["sha256"] == digest
    assert status["bytes"] == archive.stat().st_size
    assert status["uploaded"] is False and status["release"] == "scrapeyard-release-new"
    assert status["application_down_seconds"] >= 0
    assert host.status.stat().st_mode & 0o777 == 0o644
    assert "kept on the host only" in result.stdout
    # Only the project name is read from the settings; no credential is in
    # anything the run wrote or said.
    written = result.stdout + result.stderr + host.calls.read_text() + host.status.read_text()
    assert "hunter2" not in written


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ({"COPY_FAILS": "yes"}, "the backup set could not be created"),
        ({"SAVE_FAILS": "yes"}, "Redis did not save a settled copy"),
        ({"DUMP_COPY_FAILS": "yes"}, "Redis's dump could not be copied"),
        ({"STOP_FAILS": "yes"}, "could not be stopped"),
        # systemctl reports success for a stop whose own command failed.
        ({"STOP_LEAVES_IT_RUNNING": "yes"}, "is still running after scrapeyard.service was stopped"),
        # A Docker that does not answer says nothing about what is running.
        ({"PS_FAILS_ONCE_STOPPED": "yes"}, "Docker did not say whether the application has stopped"),
    ],
)
def test_application_is_started_again_when_anything_fails_after_the_stop(host, fault, message):
    host.status.parent.mkdir()
    host.status.write_text("the last completed backup\n")

    result = host.run("run", **fault)

    assert result.returncode == 2
    assert message in result.stderr
    calls = host.called("systemctl")
    assert calls.index("systemctl stop scrapeyard.service") < calls.index(
        "systemctl start --no-block scrapeyard.service"
    )
    # The start was asked for with the release lock given up, or it would have failed.
    assert not host.called("systemctl start: the release lock")
    # Nothing is copied from an application that may still be writing.
    if "STOP" in next(iter(fault)):
        assert not host.called("copy: ") and not host.called("docker exec b22 redis-cli SAVE")
    assert host.application_is_running() and not host.marker.exists()
    # Nothing unencrypted and no half-written archive stays behind, and the
    # status still describes the last backup that completed.
    assert host.left_in_backups() == set()
    assert host.status.read_text() == "the last completed backup\n"
    assert not host.encrypted()


def test_application_is_started_again_when_the_run_is_signalled_after_the_stop(host):
    # systemd signals every process of a run it stops; the copy that hangs ends with it.
    run = subprocess.Popen(
        [str(host.script), "run"],
        env=dict(host.env, COPY_HANGS="yes"),
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while not (host.state / "copy-started").exists():
            assert time.monotonic() < deadline and run.poll() is None
            time.sleep(0.05)
        assert not host.application_is_running() and host.marker.exists()
        os.killpg(run.pid, signal.SIGTERM)
        assert run.wait(timeout=30) == 143
    finally:
        if run.poll() is None:
            os.killpg(run.pid, signal.SIGKILL)

    assert host.called("systemctl")[-1] == "systemctl start --no-block scrapeyard.service"
    assert host.application_is_running() and not host.marker.exists()
    assert host.left_in_backups() == set()
    assert not host.status.exists()


def test_a_run_that_was_killed_is_made_good_by_resume_and_by_the_next_run(host):
    # What a killed run leaves: the application stopped, a marker, and a copy.
    for name in ("unit-active", "app-running"):
        (host.state / name).unlink()
    host.marker.touch()
    stale = host.backups / ".stage.killed"
    stale.mkdir(parents=True)
    (stale / "jobs.db").write_text("PLAINTEXT-OF-THE-DATABASE")
    (host.backups / ".scrapeyard-backup-20200101T000000Z.tar.zst.age.partial").touch()

    assert host.run("resume").returncode == 0
    # Not waited for: systemd holds the start back while the backup unit stops.
    assert host.called("systemctl") == ["systemctl start --no-block scrapeyard.service"]
    assert host.application_is_running() and not host.marker.exists()
    assert host.left_in_backups() == set()

    # With nothing owed, resume starts nothing: it runs after every backup.
    host.calls.unlink()
    assert host.run("resume").returncode == 0
    assert host.called("systemctl") == []

    # A run that finds the marker starts the application before anything else.
    host.marker.touch()
    (host.state / "unit-active").unlink()
    result = host.run("run")
    assert result.returncode == 0, result.stderr
    assert host.called("systemctl")[0] == "systemctl start scrapeyard.service"
    assert len(host.archives()) == 1


def test_nothing_is_stopped_while_the_application_is_busy(host):
    (host.state / "busy").write_text("1000000")

    result = host.run("run", SCRAPEYARD_BACKUP_IDLE_TIMEOUT_SECONDS="1")

    assert result.returncode == 2
    assert (
        "was not idle within 1s (its last answer: not idle: status ok, 1 running, 0 queued); "
        "nothing was stopped"
    ) in result.stderr
    # How many times it asked within the second depends on the machine.
    assert host.called("docker exec -i a11 python - witness")
    assert not host.stopped() and host.application_is_running()
    assert not host.marker.exists() and host.release_lock_is_free()
    assert host.left_in_backups() == set() and not host.status.exists()


@pytest.mark.parametrize(
    ("answer", "said"),
    [
        # Neither "idle" nor "not now": a read the application refused, say.
        ({"ASK_FAILS": "yes"}, "its last answer: a result read answered HTTP 429"),
        # Idle or not, an application that is unhealthy is not stopped and started.
        ({"UNHEALTHY": "yes"}, "its last answer: unhealthy: /health/ready answered HTTP 503"),
    ],
)
def test_only_an_idle_answer_lets_a_backup_stop_the_application(host, answer, said):
    result = host.run("run", SCRAPEYARD_BACKUP_IDLE_TIMEOUT_SECONDS="1", **answer)

    assert result.returncode == 2
    assert said in result.stderr and "nothing was stopped" in result.stderr
    assert not host.stopped() and host.application_is_running()
    assert not host.called("copy: ") and not host.encrypted()


def test_backup_waits_for_the_application_and_takes_the_witness_of_the_idle_moment(host):
    (host.state / "busy").write_text("2")

    result = host.run("run")

    assert result.returncode == 0, result.stderr
    assert len(host.called("docker exec -i a11 python - witness")) == 3
    # Why it waits is said once, not once for every question.
    assert result.stderr.count("waiting: not idle") == 1
    assert json.loads(host.members(host.archives()[0])["witness.json"]) == WITNESS


@pytest.mark.parametrize("state", ["active", "activating", "deactivating"])
def test_nothing_is_stopped_while_a_promotion_is_under_way(host, state):
    (host.state / "promotion").write_text(state)

    result = host.run("run")

    assert result.returncode == 2
    assert "a promotion is under way; nothing was stopped" in result.stderr
    assert not host.stopped() and host.application_is_running()
    assert not host.called("docker exec")


def test_a_promotion_that_begins_during_the_wait_ends_the_backup(host):
    (host.state / "busy").write_text("1000000")
    # The first question finds the application busy, and a promotion starts.
    host.tool(
        "sleep",
        '#!/bin/bash\necho activating > "$STATE/promotion"\n',
    )

    result = host.run("run")

    assert result.returncode == 2
    assert "a promotion is under way; nothing was stopped" in result.stderr
    assert len(host.called("docker exec -i a11 python - witness")) == 1
    assert not host.stopped() and host.release_lock_is_free()


def _break(host: _Host, problem: str) -> dict[str, str]:
    """Puts one thing wrong on the host and returns the environment to run with."""
    if problem == "no recipients":
        host.recipients.unlink()
    elif problem == "recipients others can write":
        host.recipients.chmod(0o664)
    elif problem == "recipients behind a link":
        real = host.recipients.with_name("real-recipients")
        host.recipients.rename(real)
        host.recipients.symlink_to(real)
    elif problem == "recipients in a directory others can write":
        host.etc.chmod(0o777)
    elif problem == "a private key among the recipients":
        host.recipients.write_text("AGE-SECRET-KEY-1QQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ\n")
        host.recipients.chmod(0o644)
    elif problem == "no recipient named":
        host.recipients.write_text("# to be filled in\n")
        host.recipients.chmod(0o644)
    elif problem == "a private key beside a public key":
        host.recipients.write_text(f"{RECIPIENT}\nAGE-SECRET-KEY-1QQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ\n")
        host.recipients.chmod(0o644)
    elif problem == "a recipient line age does not accept":
        host.recipients.write_text(f"{RECIPIENT}\nage1mistyped\n")
        host.recipients.chmod(0o644)
    elif problem == "a settings line the start could not read":
        with host.env_file.open("a") as settings:
            settings.write("SCRAPEYARD_LOG_LEVEL=INFO\n")
    elif problem == "more data than there is room for twice":
        return {"DATA_MB": "99999999"}
    elif problem == "hook the group can write":
        host.install_hook()
        host.hook.chmod(0o775)
    elif problem == "hook anyone can write":
        host.install_hook()
        host.hook.chmod(0o757)
    elif problem == "hook behind a link":
        host.install_hook()
        real = host.hook.with_name("real-upload")
        host.hook.rename(real)
        host.hook.symlink_to(real)
    elif problem == "hook that cannot be run":
        host.install_hook()
        host.hook.chmod(0o644)
    elif problem == "boot service not active":
        (host.state / "unit-active").unlink()
    elif problem == "no promoted release":
        host.current.unlink()
    elif problem == "a release others can write":
        (host.release / "source" / "scripts" / "qualification_backup.py").chmod(0o666)
    elif problem == "another release running":
        return {"RUNNING_IMAGE": "sha256:older"}
    elif problem == "data that is not a named volume":
        return {"DATA_MOUNT": "bind:"}
    elif problem == "a full disk":
        return {"SCRAPEYARD_BACKUP_MIN_FREE_MB": "9999999"}
    elif problem == "an application that cannot be asked":
        return {"CANNOT_ASK": "yes"}
    elif problem == "a setting that is not a number":
        return {"SCRAPEYARD_BACKUP_KEEP": "seven"}
    elif problem == "keeping no archive":
        return {"SCRAPEYARD_BACKUP_KEEP": "0"}
    return {}


@pytest.mark.parametrize(
    "problem",
    [
        "no recipients",
        "recipients others can write",
        "recipients behind a link",
        "recipients in a directory others can write",
        "a private key among the recipients",
        "a private key beside a public key",
        "no recipient named",
        "a recipient line age does not accept",
        "a settings line the start could not read",
        "more data than there is room for twice",
        "hook the group can write",
        "hook anyone can write",
        "hook behind a link",
        "hook that cannot be run",
        "boot service not active",
        "no promoted release",
        "a release others can write",
        "another release running",
        "data that is not a named volume",
        "a full disk",
        "an application that cannot be asked",
        "a setting that is not a number",
        "keeping no archive",
    ],
)
def test_what_cannot_be_trusted_or_used_is_refused_before_anything_is_stopped(host, problem):
    result = host.run("run", **_break(host, problem))

    assert result.returncode == 2
    assert "scrapeyard backup:" in result.stderr
    assert not host.stopped() and (host.state / "app-running").exists()
    assert not host.called("systemctl start") and not host.marker.exists()
    assert not host.encrypted() and not host.archives()
    # A hook that is refused is never run.
    assert not (host.state / "uploaded").exists()
    assert "QQQQ" not in result.stderr and "hunter2" not in result.stderr
    if problem == "a settings line the start could not read":
        # The line is named by its number, as the unit's start would name it,
        # and before the wait: nothing is waited for that would be refused.
        assert "deploy.env line 3 is not a SCRAPEYARD_ setting" in result.stderr
        assert "could not be started again" in result.stderr
        assert not host.called("docker exec")
    if problem == "more data than there is room for twice":
        assert "a backup of 99999999 MB of data" in result.stderr
    if "private key" in problem:
        # Said for what it is, whatever else the file holds.
        assert "holds a private key" in result.stderr


def test_settings_broken_during_the_wait_are_found_before_the_stop(host):
    (host.state / "busy").write_text("1")
    # The first question finds the application busy; the file is edited meanwhile.
    host.tool("sleep", f'#!/bin/bash\necho "SCRAPEYARD_LOG_LEVEL=INFO" >> {host.env_file}\n')

    result = host.run("run")

    assert result.returncode == 2
    assert "could not be started again" in result.stderr and "nothing was stopped" in result.stderr
    assert len(host.called("docker exec -i a11 python - witness")) == 2
    assert not host.stopped() and host.application_is_running() and host.release_lock_is_free()


def test_jobs_accepted_after_the_witness_are_named_and_left_out_of_the_comparison(host):
    # The lock keeps starts and promotions out between the last answer and the
    # stop, not submissions. j8 is in a project the witness does not cover.
    result = host.run("run", ACCEPTED_SINCE="j9=td21 j8=elsewhere")

    assert result.returncode == 0, result.stderr
    assert "1 job(s) were accepted after the witness was taken" in result.stderr
    (archive,) = host.archives()
    members = host.members(archive)
    assert json.loads(members["metadata.json"])["accepted_after_witness"] == ["j9"]
    # The set's manifest lists its files: reading the database left none behind.
    assert {name for name in members if name.startswith("set/")} == {
        "set/manifest.json",
        "set/payload/db/jobs.db",
    }
    host.calls.unlink()

    tested = host.run("restore-test", str(archive), "--identity", str(host.identity))

    assert tested.returncode == 0, tested.stderr
    # Exactly those jobs are left out of what the copy is asked for.
    assert (host.state / "left-out").read_text() == "j9"
    assert "Accepted after the witness was taken, in the archive and not compared: j9" in tested.stdout
    _nothing_left_of_the_restore_test(host)


def test_an_archive_names_at_most_a_thousand_late_jobs_and_its_restore_test_says_so(host):
    late = [f"j{number:04d}" for number in range(1001)]

    result = host.run("run", ACCEPTED_SINCE=" ".join(f"{job}=td21" for job in late))

    assert result.returncode == 0, result.stderr
    assert "1001 job(s) were accepted after the witness was taken" in result.stderr
    assert "the first 1000 of them: its restore test cannot compare the copy" in result.stderr
    (archive,) = host.archives()
    metadata = json.loads(host.members(archive)["metadata.json"])
    assert metadata["accepted_after_witness"] == late[:1000]
    assert metadata["accepted_after_witness_count"] == 1001
    host.calls.unlink()

    tested = host.run("restore-test", str(archive), "--identity", str(host.identity))

    assert tested.returncode == 2
    assert "1001 jobs were accepted after the witness was taken, more than the 1000 an archive names" in tested.stderr
    assert not host.called("docker run")
    _nothing_left_of_the_restore_test(host)


def test_an_archive_is_never_written_over(host):
    # The name carries the second of the copy; the clock is held still here.
    host.tool(
        "date",
        '#!/bin/bash\n'
        'if [[ "$*" == "-u +%Y-%m-%dT%H:%M:%SZ" ]]; then echo 2026-01-02T03:04:05Z; '
        'else exec /usr/bin/date "$@"; fi\n',
    )
    host.backups.mkdir(mode=0o700)
    earlier = host.backups / "scrapeyard-backup-20260102T030405Z.tar.zst.age"
    earlier.write_text("an archive made in the same second")

    result = host.run("run")

    assert result.returncode == 2
    assert "exists already; nothing is overwritten" in result.stderr
    assert earlier.read_text() == "an archive made in the same second"
    assert host.left_in_backups() == {earlier.name}
    assert host.application_is_running() and not host.status.exists()


def test_upload_hook_is_given_the_archive_and_its_failure_fails_the_run(host):
    host.install_hook('echo "$1" > "$STATE/uploaded"\nenv > "$STATE/hook-environment"\n')

    result = host.run("run")

    assert result.returncode == 0, result.stderr
    (archive,) = host.archives()
    assert (host.state / "uploaded").read_text() == f"{archive}\n"
    # A backup reads the project's name from the settings and nothing else,
    # so the hook is not handed the deployment's credentials.
    assert "hunter2" not in (host.state / "hook-environment").read_text()
    assert json.loads(host.status.read_text())["uploaded"] is True
    assert "kept on the host only" not in result.stdout

    before = host.status.read_text()
    host.install_hook('echo "the store refused it" >&2\nexit 1\n')
    for name in ("unit-active", "app-running"):
        (host.state / name).touch()
    # The archive is named by the second of its copy.
    time.sleep(1.1)
    failed = host.run("run")

    assert failed.returncode == 2
    assert "failed; the archive is on this host only" in failed.stderr
    # The archive is kept for the next attempt, the application is back, and
    # the status still names the last backup that reached the store.
    assert len(host.archives()) == 2 and host.application_is_running()
    assert host.status.read_text() == before


def test_an_archive_is_still_written_when_the_application_does_not_come_back(host):
    result = host.run("run", START_FAILS="yes")

    assert result.returncode == 2
    assert "the application did not come back ready" in result.stderr
    # The copy may be the last good one of a host that no longer starts.
    (archive,) = host.archives()
    assert host.left_in_backups() == {archive.name, archive.name + ".sha256"}
    assert not host.status.exists()
    # The start is the unit's to retry; `resume` asks for it once more.
    assert host.marker.exists()


@pytest.mark.parametrize("keep", [1, 3])
def test_pruning_keeps_the_newest_archives_and_touches_nothing_else(host, keep):
    host.backups.mkdir(mode=0o700)
    old = [f"scrapeyard-backup-2020010{day}T000000Z.tar.zst.age" for day in range(1, 8)]
    # An archive named after a clock that ran ahead sorts as the newest.
    ahead = "scrapeyard-backup-29990101T000000Z.tar.zst.age"
    for name in [*old, ahead]:
        (host.backups / name).write_text("an archive")
        (host.backups / f"{name}.sha256").write_text("its checksum")
    bystanders = {
        "notes.txt",
        "scrapeyard-backup-latest.tar.zst.age",
        "scrapeyard-backup-20200103T000000Z.tar.zst.age.bak",
        "eyebox-backup-20200103T000000Z.tar.zst.age",
    }
    for name in bystanders:
        (host.backups / name).write_text("someone else's")
    # A directory and a link that carry an archive's name are not archives.
    directory = "scrapeyard-backup-20190101T000000Z.tar.zst.age"
    (host.backups / directory).mkdir()
    link = "scrapeyard-backup-20180101T000000Z.tar.zst.age"
    (host.backups / link).symlink_to(host.backups / "notes.txt")

    result = host.run("run", SCRAPEYARD_BACKUP_KEEP=str(keep))

    assert result.returncode == 0, result.stderr
    new = json.loads(host.status.read_text())["archive"].rsplit("/", 1)[1]
    # The newest by name, and the one just written whatever its name says.
    kept = sorted({*sorted([*old, ahead, new])[-keep:], new})
    assert host.left_in_backups() == {
        *kept,
        *(f"{name}.sha256" for name in kept),
        *bystanders,
        directory,
        link,
    }
    assert (host.backups / "notes.txt").read_text() == "someone else's"


def _backed_up(host: _Host, **env: str) -> Path:
    result = host.run("run", **env)
    assert result.returncode == 0, result.stderr
    host.calls.unlink()
    return host.archives()[0]


def _nothing_left_of_the_restore_test(host: _Host) -> None:
    for name in ("isolated-app", "isolated-redis", "isolated-volumes", "network"):
        assert not (host.state / name).exists(), name
    assert not [name for name in host.left_in_backups() if name.startswith(".")]


def test_restore_test_restores_into_an_isolated_project_and_removes_it(host):
    archive = _backed_up(host)

    result = host.run("restore-test", str(archive), "--identity", str(host.identity))

    assert result.returncode == 0, result.stderr
    assert "Restore test passed" in result.stdout
    assert "5 keys in the dump, 4 loaded and 1 expired" in result.stdout
    assert "encryption envelopes under key test-key" in result.stdout
    # The network is made internal and without an address of the host's, and
    # found to be both, twice, before the application starts on it: Docker is
    # asked for the first and the kernel for the second.
    created = host.first(
        "docker network create --internal --subnet 172.30.1.0/24 "
        "--opt com.docker.network.bridge.gateway_mode_ipv4=isolated"
    )
    started = host.first("up -d --no-deps --no-build --pull never scrapeyard")
    checks = [
        i
        for i, call in enumerate(host.called())
        if call.startswith("docker network inspect --format {{.Internal}} {{.Id}} " + ISOLATED)
    ]
    assert len(checks) == 2 and created < checks[0] < checks[1] < started
    bridge = [i for i, call in enumerate(host.called()) if call == "ip -4 -o address show dev br-abababababab"]
    assert len(bridge) == 2 and checks[0] < bridge[0] < checks[1] < bridge[1] < started
    # The confinement reported is the kernel's label of the copy's first process.
    assert host.called("docker exec c33 cat /proc/1/attr/current")
    assert "confined as scrapeyard-chromium (enforce) by the kernel's label" in result.stdout
    assert (host.state / "left-out").read_text() == ""
    # Every Compose command names the test's project, runs in the release that
    # made the archive, and cannot fall back on the deployment's project name.
    composed = host.called("compose for")
    assert composed and all(
        call.startswith(f"compose for {ISOLATED}: files {host.release}/source/docker-compose.yml:")
        and "project variable unset" in call
        and call.endswith(f"in {host.release}/source")
        for call in composed
    )
    assert not [call for call in host.called("docker compose") if "--project-name scrapeyard " in call]
    isolated = yaml.load((host.state / "isolated.yml").read_text(), Loader=yaml.BaseLoader)
    assert isolated["networks"]["backend"] == {"name": f"{ISOLATED}_backend", "external": "true"}
    application = isolated["services"]["scrapeyard"]
    # Nothing of the test survives a reboot, and its application starts
    # without the probe, so no second egress policy is put on the host.
    assert application["restart"] == isolated["services"]["redis"]["restart"] == "no"
    assert application["environment"]["SCRAPEYARD_EGRESS_POLICY_PROBE_HOST"] == ""
    assert application["environment"]["SCRAPEYARD_UNTRUSTED_SUBMISSIONS"] == "false"
    # A deployment's proxy named by a host name cannot be resolved from a
    # network nothing leaves, and the application would refuse to start.
    assert application["environment"]["SCRAPEYARD_PROXY_URL"] == ""
    assert application["networks"]["backend"]["ipv4_address"] == "172.30.1.250"
    # The deployment was not stopped, and nothing of the test is left.
    assert not host.called("systemctl stop") and not host.called("systemctl start")
    assert host.application_is_running()
    _nothing_left_of_the_restore_test(host)
    assert host.left_in_backups() == {archive.name, archive.name + ".sha256"}
    assert "hunter2" not in result.stdout + result.stderr


def test_redis_keys_are_counted_in_whichever_database_holds_them(host):
    # The DSN can name another database than the first, which DBSIZE counts.
    archive = _backed_up(host, REDIS_DB="1")

    assert json.loads(host.members(archive)["metadata.json"])["redis_keys"] == 5
    result = host.run("restore-test", str(archive), "--identity", str(host.identity), REDIS_DB="1")
    assert result.returncode == 0, result.stderr
    assert "Restore test passed" in result.stdout


@pytest.mark.parametrize(
    ("fault", "message", "application_started"),
    [
        ({"NETWORK_REPORTS": "false"}, "network is not internal; the application was not started", False),
        # A Docker that does not know the option gives the bridge an address.
        ({"BRIDGE_HAS_AN_ADDRESS": "yes"}, "bridge has an address of this host's", False),
        ({"BRIDGE_IS_MISSING": "yes"}, "bridge br-abababababab could not be read", False),
        # Docker starts a container unconfined where it cannot use AppArmor.
        ({"KERNEL_LABEL": "unconfined"}, "runs as 'unconfined', not under the scrapeyard-chromium", True),
        ({"DOCKER_ROOT_FREE_MB": "5"}, "restoring into fresh volumes (1 MB, and 1024 MB beside) needs 1025 MB", False),
        ({"SET_INVALID": "yes"}, "the backup set in the archive is not valid", False),
        ({"REDIS_LOADS": "0 0"}, "the dump was made with 5 keys, but Redis loaded 0", False),
        ({"REDIS_LOADS": ""}, "Redis did not load the dump", False),
        # Redis started with its append-only file ignores a bare dump and is empty.
        ({"RESTORED_KEYS": "0"}, "the isolated Redis holds 0 keys after loading 4", False),
        ({"IMAGES_MISSING": "yes"}, "is not loaded on this host", False),
        ({"HAS_ROUTE": "yes"}, "has a route off the host", True),
    ],
)
def test_restore_test_refuses_and_cleans_up(host, fault, message, application_started):
    archive = _backed_up(host)

    result = host.run("restore-test", str(archive), "--identity", str(host.identity), **fault)

    assert result.returncode == 2
    assert message in result.stderr
    assert "Restore test passed" not in result.stdout
    started = [call for call in host.called("docker compose") if call.endswith(" never scrapeyard")]
    assert bool(started) is application_started
    if "DOCKER_ROOT_FREE_MB" in fault:
        # Found before either volume is filled.
        assert not [call for call in host.called("docker compose") if " run " in call]
    _nothing_left_of_the_restore_test(host)
    assert host.application_is_running() and not host.called("systemctl st")


def test_restore_test_unpacks_nothing_it_has_no_room_for(host):
    archive = _backed_up(host)

    result = host.run(
        "restore-test", str(archive), "--identity", str(host.identity),
        SCRAPEYARD_BACKUP_MIN_FREE_MB="9999999",
    )

    assert result.returncode == 2
    assert "unpacking the archive (1 MB, and 9999999 MB beside) needs 10000000 MB free" in result.stderr
    # Measured from the stream; nothing was written, and nothing was created.
    assert not host.called("docker network create") and not host.called("docker run")
    assert host.left_in_backups() == {archive.name, archive.name + ".sha256"}


def test_restore_test_refuses_a_room_setting_that_is_not_a_number(host):
    archive = _backed_up(host)

    result = host.run(
        "restore-test", str(archive), "--identity", str(host.identity),
        SCRAPEYARD_BACKUP_MIN_FREE_MB="1e3",
    )

    assert result.returncode == 2
    assert "SCRAPEYARD_BACKUP_MIN_FREE_MB is not a whole number" in result.stderr
    assert not host.called("docker network create") and not host.called("docker run")


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        # The network could not be removed: something of the test is left.
        ({"NETWORK_STAYS": "yes"}, "could not remove all of scrapeyard-restore-test"),
        # Docker stopped answering: nothing is known about what is left.
        ({"LISTING_FAILS_ONCE_STARTED": "yes"}, "Docker did not say what is left of scrapeyard-restore-test"),
    ],
)
def test_restore_test_does_not_say_removed_of_what_it_could_not_confirm(host, fault, message):
    archive = _backed_up(host)

    result = host.run("restore-test", str(archive), "--identity", str(host.identity), **fault)

    assert result.returncode == 2
    assert message in result.stderr
    assert "could not be removed, or Docker did not say what is left of it" in result.stderr
    assert "passed" not in result.stdout and "removed" not in result.stdout


def test_restore_test_does_not_start_when_docker_cannot_list_what_is_there(host):
    archive = _backed_up(host)

    result = host.run("restore-test", str(archive), "--identity", str(host.identity), DOCKER_PS_FAILS="yes")

    assert result.returncode == 2
    assert "Docker did not list what an earlier restore test may have left" in result.stderr
    assert not host.called("age") and not host.called("docker network create")


def test_removal_of_the_restore_tests_project_cannot_be_pointed_at_the_deployment(host):
    # No command reaches the function with the deployment's project; the guard
    # is what keeps a later change from doing so. It is run here by itself.
    function = _function(SCRIPT.read_text(encoding="utf-8"), "remove_isolated_project") + "\n}\n"
    harness = (
        'docker_within() { shift; docker "$@"; }\n'
        + function
        + 'PROJECT=$1\nremove_isolated_project\necho "ended $?"\n'
    )
    (host.state / "isolated-app").touch()

    refused = subprocess.run(
        ["bash", "-c", harness, "harness", "scrapeyard"], env=host.env, capture_output=True, text=True
    )
    assert refused.stdout == "ended 1\n" and host.called("docker") == []

    done = subprocess.run(
        ["bash", "-c", harness, "harness", ISOLATED], env=host.env, capture_output=True, text=True
    )
    assert done.stdout == "ended 0\n" and "docker rm -f -v c33" in host.called("docker")


def test_restore_test_fails_when_the_restored_application_reads_back_differently(host):
    archive = _backed_up(host)
    restored = json.loads(json.dumps(WITNESS))
    restored["jobs"].pop()
    restored["samples"]["complete"]["config_sha256"] = "not what was stored"
    (host.state / "restored.json").write_text(json.dumps(restored, indent=1, sort_keys=True) + "\n")

    result = host.run("restore-test", str(archive), "--identity", str(host.identity))

    assert result.returncode == 2
    assert "job list: 2 in the witness, 1 restored, 1 missing, 0 unexpected, 0 changed" in result.stderr
    assert "newest complete job: differs in config_sha256" in result.stderr
    assert "does not read back what the witness recorded" in result.stderr
    _nothing_left_of_the_restore_test(host)


def test_restore_test_removes_what_an_earlier_one_left_and_never_the_deployment(host):
    archive = _backed_up(host)
    for name in ("isolated-app", "isolated-volumes"):
        (host.state / name).touch()
    (host.state / "network").write_text("open")

    result = host.run("restore-test", str(archive), "--identity", str(host.identity))

    assert result.returncode == 0, result.stderr
    assert "Removing what an earlier restore test left" in result.stdout
    # The leftovers go before the new network is made; `called` fails the test
    # if a container or volume of the deployment was ever removed.
    assert host.first("docker rm -f -v c33") < host.first("docker network create")
    _nothing_left_of_the_restore_test(host)


@pytest.mark.parametrize("problem", ["group can read", "a link", "missing"])
def test_restore_test_refuses_an_identity_others_can_read(host, problem):
    archive = _backed_up(host)
    if problem == "group can read":
        host.identity.chmod(0o640)
    elif problem == "a link":
        real = host.identity.with_name("real-identity")
        host.identity.rename(real)
        host.identity.symlink_to(real)
    else:
        host.identity.unlink()

    result = host.run("restore-test", str(archive), "--identity", str(host.identity))

    assert result.returncode == 2
    assert "nobody else can read" in result.stderr
    assert not host.called("age") and not host.called("docker network create")


def _archive(host: _Host, build) -> Path:
    """An archive as anyone holding the public key could make one."""
    packed = io.BytesIO()
    with tarfile.open(fileobj=packed, mode="w") as tar:
        build(tar)
    host.backups.mkdir(mode=0o700, exist_ok=True)
    archive = host.backups / "scrapeyard-backup-20200101T000000Z.tar.zst.age"
    archive.write_bytes(b"age-stand-in\n" + packed.getvalue())
    return archive


def _member(tar: tarfile.TarFile, name: str, content: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(content)
    tar.addfile(info, io.BytesIO(content))


def _honest(tar: tarfile.TarFile, **changes: object) -> None:
    manifest = b'{"format": "scrapeyard-backup-v1"}\n'
    metadata = {
        "format": "scrapeyard-host-backup-v1",
        "created_at": "2020-01-01T00:00:00Z",
        "release": "scrapeyard-release-new",
        "redis_keys": 5,
        "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
        "accepted_after_witness": [],
        **changes,
    }
    _member(tar, "metadata.json", json.dumps(metadata).encode())
    _member(tar, "witness.json", json.dumps(WITNESS).encode())
    _member(tar, "redis/dump.rdb", b"REDIS-DUMP")
    _member(tar, "set/manifest.json", manifest)


def _with_a_link(tar: tarfile.TarFile) -> None:
    _honest(tar)
    link = tarfile.TarInfo("set/payload")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc"
    tar.addfile(link)


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (_with_a_link, "neither a file nor a directory"),
        (lambda tar: _honest(tar, release="../../etc"), "not one this script writes"),
        (lambda tar: _honest(tar, release="scrapeyard-release-gone"), "which is not retained"),
        (lambda tar: _honest(tar, manifest_sha256="0" * 64), "not the one the archive was made with"),
        (lambda tar: _honest(tar, format="something-else"), "not one this script writes"),
        # The jobs named are passed on and printed, and the archive names them.
        (lambda tar: _honest(tar, accepted_after_witness=["j9; reboot"]), "not one this script writes"),
        (lambda tar: _honest(tar, accepted_after_witness="j9"), "not one this script writes"),
        (lambda tar: _honest(tar, redis_keys="08"), "not one this script writes"),
        # The count is a whole number no smaller than the names it counts.
        (lambda tar: _honest(tar, accepted_after_witness_count=1.5), "not one this script writes"),
        (lambda tar: _honest(tar, accepted_after_witness_count=True), "not one this script writes"),
        (lambda tar: _honest(tar, accepted_after_witness_count=-1), "not one this script writes"),
        (lambda tar: _honest(tar, accepted_after_witness=["j9"], accepted_after_witness_count=0),
         "not one this script writes"),
        (lambda tar: _honest(tar, accepted_after_witness_count=10**12 + 1), "not one this script writes"),
        (lambda tar: _member(tar, "witness.json", b"{}"), "the archive has no metadata.json"),
    ],
)
@pytest.mark.parametrize("action", ["restore-test", "restore"])
def test_an_archive_is_not_trusted_for_having_been_decrypted(host, build, message, action):
    archive = _archive(host, build)
    if action == "restore":
        # Onto a host with no deployment: nothing running, no containers, no volumes.
        (host.state / "unit-active").unlink()

    # Encryption to a public key says nothing about who made an archive.
    result = host.run(action, str(archive), "--identity", str(host.identity), NO_DEPLOYMENT="yes")

    assert result.returncode == 2
    assert message in result.stderr
    assert not host.called("docker run") and not host.called("docker compose --progress")
    assert not [name for name in host.left_in_backups() if name.startswith(".")]


def _from_before_the_review(tar: tarfile.TarFile) -> None:
    # Archives written before 2026-10-14 had no accepted_after_witness.
    manifest = b'{"format": "scrapeyard-backup-v1"}\n'
    metadata = {
        "format": "scrapeyard-host-backup-v1",
        "created_at": "2020-01-01T00:00:00Z",
        "release": "scrapeyard-release-new",
        "redis_keys": 5,
        "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
    }
    _member(tar, "metadata.json", json.dumps(metadata).encode())
    _member(tar, "witness.json", json.dumps(WITNESS).encode())
    _member(tar, "redis/dump.rdb", b"REDIS-DUMP")
    _member(tar, "set/manifest.json", manifest)


def test_an_archive_from_before_the_review_is_refused_by_the_restore_test_saying_why(host):
    archive = _archive(host, _from_before_the_review)

    tested = host.run("restore-test", str(archive), "--identity", str(host.identity), NO_DEPLOYMENT="yes")

    assert tested.returncode == 2
    assert "names no jobs accepted after its witness, as backup scripts did before 2026-10-14" in tested.stderr
    assert "not one this script writes" not in tested.stderr
    assert not host.called("docker run")

    # A restore compares nothing, so the missing list does not stop it.
    (host.state / "unit-active").unlink()
    restored = host.run("restore", str(archive), "--identity", str(host.identity), NO_DEPLOYMENT="yes")
    assert "names no jobs accepted after its witness" not in restored.stderr
    assert "not one this script writes" not in restored.stderr
    assert "Restored into the volumes of" in restored.stdout, restored.stderr


def test_a_restore_takes_an_archive_that_could_not_name_every_late_job(host):
    late = [f"j{number:04d}" for number in range(1000)]
    archive = _archive(
        host, lambda tar: _honest(tar, accepted_after_witness=late, accepted_after_witness_count=1500)
    )
    (host.state / "unit-active").unlink()

    restored = host.run("restore", str(archive), "--identity", str(host.identity), NO_DEPLOYMENT="yes")

    assert "more than the 1000 an archive names" not in restored.stderr
    assert "Restored into the volumes of" in restored.stdout, restored.stderr


def test_restore_fills_the_volumes_of_a_host_that_has_none(host):
    archive = _backed_up(host)
    for name in ("unit-active", "app-running"):
        (host.state / name).unlink()

    result = host.run("restore", str(archive), "--identity", str(host.identity), NO_DEPLOYMENT="yes")

    assert result.returncode == 0, result.stderr
    assert f"link {host.current} to {host.release}, then systemctl enable --now" in result.stdout
    # A rebuilt host has not loaded the release or its AppArmor profile yet.
    assert host.called("release_artifact") == [f"release_artifact load {host.release}"]
    assert host.called("apparmor") == ["apparmor install"]
    composed = host.called("compose for")
    assert len(composed) == 2 and all(
        call.startswith(
            f"compose for scrapeyard: files {host.release}/source/docker-compose.yml:{host.release}/images.json,"
        )
        for call in composed
    )
    # It starts nothing and removes nothing: the operator promotes the release.
    assert not host.called("systemctl st")
    assert not [call for call in host.called("docker compose") if " up " in call]
    assert not host.called("docker rm") and not host.called("docker volume rm")
    assert not host.called("docker network")
    assert not [name for name in host.left_in_backups() if name.startswith(".")]


@pytest.mark.parametrize("found", ["the unit running", "the unit starting", "containers", "volumes"])
def test_restore_replaces_nothing(host, found):
    archive = _backed_up(host)
    env = {}
    if found == "the unit starting":
        # A start that is under way has not created its containers yet.
        (host.state / "unit-state").write_text("activating\n")
        env["NO_DEPLOYMENT"] = "yes"
    if found != "the unit running":
        (host.state / "unit-active").unlink()
    if found == "volumes":
        env["NO_DEPLOYMENT"] = "yes"
        (host.state / "deployment-volumes").touch()

    result = host.run("restore", str(archive), "--identity", str(host.identity), **env)

    assert result.returncode == 2
    assert "replaces nothing" in result.stderr
    assert not host.called("age") and not host.called("docker compose --progress")
    assert not host.called("docker rm") and not host.called("docker volume rm")


def test_two_runs_cannot_share_the_work_directory(host):
    host.lock_dir.mkdir(exist_ok=True)
    with (host.lock_dir / "backup.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        result = host.run("run")

    assert result.returncode == 2
    assert "another backup, restore test or restore is running" in result.stderr
    assert not host.stopped()


CLIENT, MONITOR = "client-secret-0123456789", "monitor-secret-0123456789"
IDLE = {"status": "ok", "workers": {"active_tasks": 0, "queue_depths": {"high": 0, "normal": 0, "low": 0}}}
BUSY = {"status": "ok", "workers": {"active_tasks": 1, "queue_depths": {"high": 0, "normal": 2, "low": 0}}}


def _job(job_id: str, project: str, status: str, last_run_at: str) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "project": project,
        "name": job_id,
        "status": status,
        "created_at": "2026-10-01T00:00:00+00:00",
        "last_run_at": last_run_at,
    }


class _Api(ThreadingHTTPServer):
    """The routes the questions use, as the application answers them."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _ApiHandler)
        self.jobs = [
            _job("old-complete", "td21", "complete", "2026-10-02T00:00:00+00:00"),
            _job("new-complete", "td21", "complete", "2026-10-03T00:00:00+00:00"),
            _job("the-failed", "td21", "failed", "2026-10-04T00:00:00+00:00"),
            _job("elsewhere", "other", "failed", "2026-10-09T00:00:00+00:00"),
        ]
        # One answer for each readiness question; the last one is repeated.
        self.readiness: list[tuple[int, dict[str, Any]]] = [(200, IDLE)]
        self.result_status: dict[str, int] = {}
        self.list_status = 200
        self.default_page, self.largest_page = 2, 500
        self.asked: list[str] = []
        self.clock = 0

    def __enter__(self) -> _Api:
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.shutdown()
        self.server_close()


class _ApiHandler(BaseHTTPRequestHandler):
    server: _Api

    def log_message(self, *_args: Any) -> None:
        pass

    def _send(self, status: int, body: Any, headers: dict[str, str] | None = None) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(payload)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        api, url, key = self.server, urlsplit(self.path), self.headers.get("X-API-Key")
        query = {name: values[0] for name, values in parse_qs(url.query).items()}
        api.asked.append(self.path)
        if url.path == "/health/ready":
            if key != MONITOR:
                return self._send(403, {"error": "forbidden"})
            status, body = api.readiness.pop(0) if len(api.readiness) > 1 else api.readiness[0]
            return self._send(status, body)
        if key != CLIENT:
            return self._send(403, {"error": "forbidden"})
        if url.path == "/jobs":
            # The client's credential is limited to one project and must name it.
            if query.get("project") != "td21":
                return self._send(403, {"error": "Project-scoped callers must provide a project filter"})
            if api.list_status != 200:
                return self._send(api.list_status, {"error": "refused"})
            limit, offset = int(query.get("limit", api.default_page)), int(query.get("offset", 0))
            if limit > api.largest_page:
                return self._send(400, {"error": "Invalid 'limit'"})
            mine = [job for job in api.jobs if job["project"] == "td21"]
            more = offset + limit < len(mine)
            headers = {"X-Scrapeyard-Has-More": "true" if more else "false"}
            if more:
                headers["X-Scrapeyard-Next-Offset"] = str(offset + limit)
            return self._send(200, mine[offset : offset + limit], headers)
        kind, job_id = url.path.strip("/").split("/")
        job = next(job for job in api.jobs if job["job_id"] == job_id)
        if kind == "jobs":
            # The next run is worked out from the clock, and differs at every read.
            api.clock += 1
            return self._send(
                200, {**job, "config_yaml": f"name: {job_id}\n", "next_run_at": f"clock-{api.clock}", "runs": []}
            )
        status = api.result_status.get(job_id, 200)
        self._send(status, {"job_id": job_id, "status": job["status"], "data": {"b": 2, "a": 1}})


def _ask(tmp_path: Path, api: _Api, *question: str, **env: str) -> subprocess.CompletedProcess[str]:
    """Runs the script's in-container program itself, against the stand-in."""
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index("python - \"$@\" <<'PY'\n") + len("python - \"$@\" <<'PY'\n")
    program = text[start : text.index("\nPY\n}\n", start)]
    address = 'API = "http://127.0.0.1:8420"'
    assert program.count(address) == 1
    path = tmp_path / "ask.py"
    path.write_text(program.replace(address, f'API = "http://127.0.0.1:{api.server_port}"'))
    credentials = {
        "client": {"secret": CLIENT, "scopes": ["submit", "read"], "projects": ["td21"]},
        "monitor": {"secret": MONITOR, "scopes": ["health-detail"]},
    }
    settings = {
        "PATH": os.environ["PATH"],
        "SCRAPEYARD_API_CREDENTIALS": json.dumps(credentials),
        "SCRAPEYARD_ADMIN_READ_MAX_LIMIT": "500",
        # A proxy in the container's environment is not used to reach itself.
        "http_proxy": "http://127.0.0.1:9",
        **env,
    }
    return subprocess.run(
        [sys.executable, str(path), *question], env=settings, capture_output=True, text=True, timeout=60
    )


def test_witness_is_what_the_api_returns_for_the_jobs_a_credential_may_list(tmp_path):
    with _Api() as api:
        asked = _ask(tmp_path, api, "witness")
        again = _ask(tmp_path, api, "witness")

    assert asked.returncode == 0, asked.stderr
    witness = json.loads(asked.stdout)
    # The jobs of the one project the client's credential covers, and the
    # newest of each kind among them, each read with that credential.
    assert witness["projects"] == ["td21"]
    assert [job["job_id"] for job in witness["jobs"]] == ["new-complete", "old-complete", "the-failed"]
    assert witness["samples"]["complete"]["job_id"] == "new-complete"
    assert witness["samples"]["failed"]["job_id"] == "the-failed"
    result = json.dumps({"job_id": "new-complete", "status": "complete", "data": {"a": 1, "b": 2}}, sort_keys=True)
    assert witness["samples"]["complete"]["result_status"] == 200
    assert witness["samples"]["complete"]["result_sha256"] == hashlib.sha256(
        result.replace(", ", ",").replace(": ", ":").encode()
    ).hexdigest()
    assert witness["samples"]["complete"]["config_sha256"] == hashlib.sha256(b"name: new-complete\n").hexdigest()
    # One page, at the largest size the API allows: the requests count
    # towards the client's own rate limit.
    assert [path for path in api.asked if path.startswith("/jobs?")] == [
        "/jobs?limit=500&offset=0&project=td21"
    ] * 2
    # What depends on the clock is left out, so two readings are the same text.
    assert again.stdout == asked.stdout
    assert CLIENT not in asked.stdout + asked.stderr and MONITOR not in asked.stdout + asked.stderr


@pytest.mark.parametrize(("largest", "pages"), [("2", 2), ("", 2), ("0", 2), ("1", 3)])
def test_witness_follows_the_job_list_to_its_last_page(tmp_path, largest, pages):
    with _Api() as api:
        asked = _ask(tmp_path, api, "witness", SCRAPEYARD_ADMIN_READ_MAX_LIMIT=largest)

    assert asked.returncode == 0, asked.stderr
    assert len(json.loads(asked.stdout)["jobs"]) == 3
    listed = [path for path in api.asked if path.startswith("/jobs?")]
    assert len(listed) == pages
    # Without a usable setting no size is asked for, and the API's own is taken.
    assert all(("limit=" in path) is (largest in ("1", "2")) for path in listed)


@pytest.mark.parametrize(
    ("status", "outcome"),
    [(404, 0), (429, 4), (500, 4), (202, 4), (403, 4)],
)
def test_only_a_result_or_its_absence_is_recorded_as_a_jobs_result(tmp_path, status, outcome):
    with _Api() as api:
        api.result_status["the-failed"] = status
        asked = _ask(tmp_path, api, "witness")

    assert asked.returncode == outcome
    if outcome == 0:
        # 404 is the API's answer for a job that has no result.
        sample = json.loads(asked.stdout)["samples"]["failed"]
        assert sample["result_status"] == 404 and sample["result_bytes"] == 0
    else:
        # A refusal is not the job's result: a witness holding it would have
        # a restore compare one refusal with another, and pass.
        assert asked.stdout == ""
        assert asked.stderr.strip() == f"a result read answered HTTP {status}"


@pytest.mark.parametrize(
    ("readiness", "outcome", "said"),
    [
        ([(200, BUSY)], 3, "not idle: status ok, 1 running, 2 queued"),
        ([(200, {**IDLE, "status": "degraded"})], 3, "not idle: status degraded, 0 running, 0 queued"),
        ([(503, {"status": "unhealthy"})], 3, "unhealthy: /health/ready answered HTTP 503"),
        ([(500, {})], 3, "not ready: /health/ready answered HTTP 500"),
        # Idle when asked, and a job accepted while the lists were read.
        ([(200, IDLE), (200, BUSY)], 3, "not idle: status ok, 1 running, 2 queued"),
    ],
)
def test_no_witness_is_given_of_an_application_that_is_not_idle(tmp_path, readiness, outcome, said):
    with _Api() as api:
        api.readiness = list(readiness)
        asked = _ask(tmp_path, api, "witness")

    assert asked.returncode == outcome
    assert asked.stdout == "" and asked.stderr.strip() == said


def test_a_refused_job_list_and_a_missing_credential_are_told_apart(tmp_path):
    with _Api() as api:
        api.list_status = 429
        refused = _ask(tmp_path, api, "witness")
        unreadable = _ask(
            tmp_path, api, "witness",
            SCRAPEYARD_API_CREDENTIALS=json.dumps({"client": {"secret": CLIENT, "scopes": ["read"]}}),
        )
        # It gave an empty witness, which every restore test then matched.
        unlisted = _ask(
            tmp_path, api, "witness",
            SCRAPEYARD_API_CREDENTIALS=json.dumps(
                {"monitor": {"secret": MONITOR, "scopes": ["health-detail"]}}
            ),
        )

    # "Anything else", which the wait asks again; and what waiting cannot cure.
    assert refused.returncode == 4 and refused.stderr.strip() == "the job list answered HTTP 429"
    assert unreadable.returncode == 2 and "holds the health-detail scope" in unreadable.stderr
    assert unlisted.returncode == 2 and "holds the read scope" in unlisted.stderr
    assert refused.stdout == unreadable.stdout == unlisted.stdout == ""


def test_a_restored_copy_is_asked_when_ready_and_without_the_jobs_accepted_late(tmp_path):
    with _Api() as api:
        original = _ask(tmp_path, api, "witness")
        # In the copy, a job accepted after the witness is queued and is run.
        api.jobs.append(_job("late", "td21", "failed", "2026-10-05T00:00:00+00:00"))
        api.readiness = [(200, BUSY)]
        with_it = _ask(tmp_path, api, "restored")
        without = _ask(tmp_path, api, "restored", "late")
        api.readiness = [(503, {"status": "unhealthy"})]
        unhealthy = _ask(tmp_path, api, "restored", "late")

    assert original.returncode == with_it.returncode == without.returncode == 0, without.stderr
    # Left out of the list and of the choice of the newest failed job, the
    # late job leaves the copy's reading the same text as the witness. Not
    # left out, it is a difference like any other.
    assert without.stdout == original.stdout
    assert json.loads(with_it.stdout)["samples"]["failed"]["job_id"] == "late"
    assert unhealthy.returncode == 3 and unhealthy.stdout == ""


def _function(text: str, name: str) -> str:
    start = text.index(f"\n{name}() {{\n")
    return text[start : text.index("\n}\n", start)]


def test_checks_shared_with_the_release_script_are_the_same_text():
    # Both are installed copies, so neither can read the other's functions.
    backup, manage = SCRIPT.read_text(encoding="utf-8"), MANAGE.read_text(encoding="utf-8")
    for name in (
        "require_private_settings",
        "require_project_name",
        "load_settings",
        "load_project_name",
        "check_ancestors",
        "check_release",
        "promotion_is_under_way",
    ):
        assert _function(backup, name) == _function(manage, name), name
    for constant in ("ENV_FILE=/etc/scrapeyard/deploy.env", "LOCK_DIR=/run/scrapeyard", "PROMOTE_UNIT=scrapeyard-promote"):
        assert constant in backup and constant in manage
    # The lock a backup holds over its stop is the one a start or promotion takes.
    assert 'exec 7>>"$LOCK_DIR/release.lock"' in backup and 'exec 7>>"$LOCK_DIR/release.lock"' in manage


def test_restore_test_network_cannot_be_the_deployments():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    (deployed,) = compose["networks"]["backend"]["ipam"]["config"]
    prefix = re.search(r"^RESTORE_NETWORK_PREFIX=(\S+)$", SCRIPT.read_text(encoding="utf-8"), re.M)
    assert prefix is not None
    isolated = ipaddress.ip_network(f"{prefix.group(1)}.0/24")
    assert not isolated.overlaps(ipaddress.ip_network(deployed["subnet"]))
    assert isolated.is_private


def _unit(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, delimiters=("=",))
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read_string(path.read_text(encoding="utf-8"))
    return parser


def _seconds(span: str) -> int:
    units = {"h": 3600, "min": 60, "s": 1, "": 1}
    number, unit = re.fullmatch(r"(\d+)(h|min|s|)", span).groups()  # type: ignore[union-attr]
    return int(number) * units[unit]


def test_backup_unit_runs_the_installed_script_and_always_ends_with_resume():
    unit = _unit(SERVICE)
    service = unit["Service"]
    assert service["Type"] == "oneshot"
    assert service["ExecStart"] == "/usr/local/sbin/scrapeyard-backup run"
    # Runs after every run, however it ended: a killed run cannot start the
    # application itself.
    assert service["ExecStopPost"] == "/usr/local/sbin/scrapeyard-backup resume"
    # A stop must reach the copy that hangs, not only the script waiting for it.
    assert service["KillMode"] == "control-group"
    # A failed backup is not tried again at once: every attempt stops the application.
    assert "Restart" not in service
    assert "EnvironmentFile" not in service
    # Ordered after the application's unit, and tied to it by nothing else: a
    # failed or stopped backup must not stop or restart the application.
    assert "scrapeyard.service" in unit["Unit"]["After"].split()
    assert not {"Requires", "Wants", "BindsTo", "PartOf", "Conflicts", "Upholds"} & set(unit["Unit"])
    assert unit["Unit"]["ConditionPathExists"] == "/srv/scrapeyard-releases/current/manifest.json"
    # Above the script's own limits together, so it ends only a run that hangs.
    script = SCRIPT.read_text(encoding="utf-8")
    limits = [
        int(re.search(rf"^{name}=\$\{{SCRAPEYARD_BACKUP_\w+:-(\d+)\}}$", script, re.M).group(1))  # type: ignore[union-attr]
        for name in ("IDLE_TIMEOUT", "SNAPSHOT_TIMEOUT", "UPLOAD_TIMEOUT")
    ]
    boot_service = _unit(Path("security/systemd/scrapeyard.service"))["Service"]
    starting = int(boot_service["TimeoutStartSec"]) + int(boot_service["TimeoutStopSec"])
    assert _seconds(service["TimeoutStartSec"]) > sum(limits) + starting


def test_backup_timer_runs_daily_at_a_fixed_utc_time_clear_of_the_reboot_window():
    timer = _unit(TIMER)
    hour, minute = re.fullmatch(
        r"\*-\*-\* (\d\d):(\d\d):00 UTC", timer["Timer"]["OnCalendar"]
    ).groups()  # type: ignore[union-attr]
    start = int(hour) * 60 + int(minute)
    # The host reboots for security updates at 08:00 UTC. A run waits up to an
    # hour to begin and must be long over by then.
    assert start + 180 <= 8 * 60 or start >= 9 * 60
    assert timer["Timer"]["Persistent"] == "true"
    assert timer["Timer"]["RandomizedDelaySec"] == "0"
    assert timer["Install"]["WantedBy"] == "timers.target"
    # A timer starts the service of its own name.
    assert "Unit" not in timer["Timer"] and TIMER.stem == SERVICE.stem
