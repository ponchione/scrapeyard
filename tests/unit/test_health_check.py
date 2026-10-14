"""The host check (security/health-check.py): what it finds and what it sends."""

from __future__ import annotations

import datetime
import importlib.util
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path("security/health-check.py")
SETTING_SECRET = "settings-secret-0123456789"
PING_TOKEN = "ping-token-4f1e9c"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("scrapeyard_health_check", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Dataclasses look their module up by name while it is being executed.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Receiver(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _ReceiverHandler)
        self.received: list[tuple[str, dict[str, str], str]] = []

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"


class _ReceiverHandler(BaseHTTPRequestHandler):
    server: _Receiver

    def log_message(self, *_args: Any) -> None:
        pass

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        self.server.received.append((self.path, dict(self.headers), body))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        # The application's readiness, for the in-container probe.
        self.server.received.append((self.path, dict(self.headers), ""))
        payload = json.dumps(
            {
                "status": "unhealthy",
                "workers": {"active_tasks": 1, "max_concurrent": 2, "queue_depths": {"high": 0, "normal": 3, "low": None}},
                "dependencies": {"redis": {"ok": False, "detail": "ConnectionError"}, "disk": {"ok": True}},
                "background_tasks": {"worker": {"ok": False}, "scheduler": {"ok": True}},
            }
        ).encode()
        self.send_response(503)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def receiver():
    server = _Receiver()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


class Host:
    """The script's view of a host: files under a temporary root, Docker and systemd faked."""

    def __init__(self, module: ModuleType, root: Path) -> None:
        self.module = module
        self.root = root
        self.ready = {"code": 200, "asked": "/health/ready", "status": "ok", "failing": [], "active": 0, "max": 2, "queued": 0}
        self.restart_count = 0
        self.failed_units = ""
        self.unit_states: dict[str, str] = {}

    def backup(self, *, hours_ago: float) -> None:
        completed = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours_ago)
        self.module.BACKUP_STATUS.write_text(json.dumps({
            "completed_at": completed.strftime("%Y-%m-%dT%H:%M:%SZ"), "bytes": 1849686, "uploaded": False,
        }))

    def run(self, command: list[str], *, stdin: str | None = None) -> tuple[int, str]:
        if command[:2] == ["docker", "ps"]:
            assert "label=com.docker.compose.project=sy-prod" in command
            return 0, "app1\nredis1\nprobe1\n"
        if command[:2] == ["docker", "inspect"]:
            return 0, json.dumps([
                {"Id": "app1", "RestartCount": self.restart_count,
                 "Config": {"Labels": {"com.docker.compose.service": "scrapeyard"}},
                 "State": {"Status": "running", "Health": {"Status": "healthy"}}},
                {"Id": "redis1", "RestartCount": 0,
                 "Config": {"Labels": {"com.docker.compose.service": "redis"}}, "State": {"Status": "running"}},
                {"Id": "probe1", "RestartCount": 0,
                 "Config": {"Labels": {"com.docker.compose.service": "egress-probe"}}, "State": {"Status": "running"}},
            ])
        if command[:2] == ["docker", "exec"]:
            assert command[2:] == ["-i", "app1", "python", "-"] and stdin == self.module.READINESS_PROBE
            return 0, json.dumps(self.ready) + "\n"
        if command[:2] == ["systemctl", "is-active"]:
            return 0, self.unit_states.get(command[2], "active") + "\n"
        if command[:3] == ["systemctl", "list-units", "--failed"]:
            return 0, self.failed_units
        if command[:2] == ["systemctl", "show"]:
            return 0, ("loaded" if "LoadState" in command[2] else "0") + "\n"
        raise AssertionError(f"unexpected command {command}")


@pytest.fixture
def host(tmp_path, monkeypatch) -> Host:
    module = _load()
    root = tmp_path / "root"
    for directory in ("etc/scrapeyard", "var/lib/scrapeyard", "var/backups/scrapeyard"):
        (root / directory).mkdir(parents=True, mode=0o755)
    for directory in (root, root / "etc", root / "var", root / "var/lib", root / "var/backups"):
        directory.chmod(0o755)
    settings = root / "etc/scrapeyard/deploy.env"
    settings.write_text(
        f"SCRAPEYARD_API_CREDENTIALS='{SETTING_SECRET}'\nCOMPOSE_PROJECT_NAME='sy-prod'\n"
    )
    settings.chmod(0o600)
    paths = {
        "SETTINGS_FILE": settings,
        "PING_FILE": root / "etc/scrapeyard/health-ping",
        "HOOK_FILE": root / "etc/scrapeyard/health-hook",
        "STATE_FILE": root / "var/lib/scrapeyard/health-status.json",
        "BACKUP_STATUS": root / "var/lib/scrapeyard/backup-status.json",
        "POLICY_RECORD": root / "var/lib/scrapeyard/policy-restored.log",
        "DISK_PATHS": (root,),
        "TRUSTED_OWNER": os.getuid(),
        "TRUSTED_ROOT": root,
        "PING_SCHEMES": frozenset({"http", "https"}),
    }
    for name, value in paths.items():
        monkeypatch.setattr(module, name, value)
    fake = Host(module, root)
    monkeypatch.setattr(module, "run", fake.run)
    fake.backup(hours_ago=3)
    return fake


def _check(host: Host, capsys, *args: str) -> tuple[str, dict[str, Any]]:
    assert host.module.main(["--disk-min-free-percent", "0", "--disk-min-free-gib", "0", *args]) == 0
    out = capsys.readouterr().out
    state_file = host.module.STATE_FILE
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    return out, state


def _ping_file(host: Host, text: str, mode: int = 0o600) -> None:
    host.module.PING_FILE.write_text(text)
    host.module.PING_FILE.chmod(mode)


def test_a_healthy_host_pings_success_and_tells_ntfy_once(host, receiver, capsys):
    _ping_file(host, f"# comment\nhealthchecks {receiver.url}/{PING_TOKEN}\nntfy {receiver.url}/topic-{PING_TOKEN}\n")

    out, state = _check(host, capsys)
    _check(host, capsys)

    assert "OK (8 checks)" in out
    assert "ok   application: ok, 0 of 2 workers busy, 0 queued" in out
    assert "ok   backup: last completed 3.0 h ago, 1.8 MB, kept on this host only; timer active" in out
    paths = [path for path, _headers, _body in receiver.received]
    # healthchecks every run; ntfy only when the state changes.
    assert paths == [f"/{PING_TOKEN}", f"/topic-{PING_TOKEN}", f"/{PING_TOKEN}"]
    assert receiver.received[0][2].startswith("Scrapeyard host ")
    assert state["status"] == "ok"
    # The ping URLs are credentials of their own; only their host is printed.
    assert PING_TOKEN not in out
    assert SETTING_SECRET not in out + json.dumps(state)


def test_a_failure_pings_fail_and_tells_ntfy_with_what_failed(host, receiver, capsys):
    _ping_file(host, f"healthchecks {receiver.url}/{PING_TOKEN}\nntfy {receiver.url}/topic\n")
    _check(host, capsys)
    host.ready = {"code": 503, "asked": "/health/ready", "status": "unhealthy", "failing": ["redis", "worker"]}
    host.failed_units = "scrapeyard-policy-watch.service loaded failed failed Scrapeyard host policy check\n"
    receiver.received.clear()

    out, state = _check(host, capsys)

    assert "FAILING (2 of 8 checks)" in out
    assert "FAIL application: not ready: HTTP 503, failing: redis, worker; Docker says healthy" in out
    assert "FAIL units: failed: scrapeyard-policy-watch.service" in out
    fail_ping, ntfy = receiver.received
    assert fail_ping[0] == f"/{PING_TOKEN}/fail" and "FAIL application" in fail_ping[2]
    assert ntfy[0] == "/topic" and ntfy[1]["Priority"] == "high" and "FAIL units" in ntfy[2]
    assert state["status"] == "fail"


def test_restarts_and_policy_records_fail_once_for_what_is_new(host, capsys):
    record = host.module.POLICY_RECORD
    record.write_text("2026-10-14T09:00:00Z INPUT has no jump\n")
    out, _state = _check(host, capsys)
    assert "ok   restarts: first run; counts recorded" in out
    assert "ok   policy-watch: timer active; no new record line" in out

    host.restart_count = 2
    with record.open("a") as appended:
        appended.write("2026-10-14T10:00:00Z NOT PUT BACK: DOCKER-USER has no jump\n")
    out, _state = _check(host, capsys)
    assert "FAIL restarts: scrapeyard restarted 2 time(s)" in out
    assert "FAIL policy-watch: timer active; 1 new record line(s): 2026-10-14T10:00:00Z NOT PUT BACK" in out

    out, _state = _check(host, capsys)
    assert "ok   restarts: none since the last check" in out
    assert "ok   policy-watch: timer active; no new record line" in out


@pytest.mark.parametrize(
    ("hours_ago", "timer", "expected"),
    [
        (30, "active", "FAIL backup: last completed 30.0 h ago"),
        (3, "inactive", "FAIL backup: last completed 3.0 h ago, 1.8 MB, kept on this host only; timer inactive"),
        (None, "active", "FAIL backup: no backup has completed; its timer is active"),
    ],
)
def test_an_old_or_missing_backup_or_a_stopped_timer_fails(host, capsys, hours_ago, timer, expected):
    host.unit_states["scrapeyard-backup.timer"] = timer
    if hours_ago is None:
        host.module.BACKUP_STATUS.unlink()
    else:
        host.backup(hours_ago=hours_ago)

    out, _state = _check(host, capsys)

    assert expected in out


def test_low_disk_fails(host, capsys):
    assert host.module.main(["--disk-min-free-percent", "101"]) == 0

    assert "LOW" in capsys.readouterr().out.split("disk:", 1)[1].splitlines()[0]


def test_without_a_hook_it_says_what_it_would_have_sent(host, capsys):
    out, state = _check(host, capsys)

    assert "ok   hooks: none installed; nothing is sent" in out
    assert "Nothing sent; with a hook installed, this would have been: Scrapeyard host" in out
    assert state["status"] == "ok"


@pytest.mark.parametrize(
    ("text", "mode", "expected"),
    [
        (f"healthchecks http://127.0.0.1:9/{PING_TOKEN}\n", 0o644, "must be root's alone (mode 600)"),
        (f"pushover http://127.0.0.1:9/{PING_TOKEN}\n", 0o600, "line 1 is not `healthchecks https://...`"),
    ],
)
def test_an_unsafe_or_unreadable_ping_file_sends_nothing_and_fails(host, receiver, capsys, text, mode, expected):
    _ping_file(host, text, mode)

    out, state = _check(host, capsys)

    assert f"FAIL hooks: {host.module.PING_FILE} " in out and expected in out
    assert receiver.received == []
    assert PING_TOKEN not in out
    assert state["status"] == "fail"


def test_the_command_hook_gets_the_state_and_the_report(host, tmp_path, capsys):
    calls = tmp_path / "calls"
    hook = host.module.HOOK_FILE
    hook.write_text(f'#!/bin/sh\nprintf "%s %s\\n" "$1" "$2" >> {calls}\ncat >> {calls}\necho hook-output-secret\n')
    hook.chmod(0o755)

    out, _state = _check(host, capsys)
    _check(host, capsys)

    lines = calls.read_text().splitlines()
    assert lines[0] == "ok changed" and lines[1].startswith("Scrapeyard host ")
    assert "ok unchanged" in lines
    assert f"{hook} ok changed: exit status 0" in out
    assert "hook-output-secret" not in out

    hook.chmod(0o775)
    calls.unlink()
    out, _state = _check(host, capsys)
    assert "FAIL hooks:" in out and "writable by root only; it is not run" in out
    assert not calls.exists()


def test_a_dry_run_sends_nothing_and_keeps_the_status_file(host, receiver, capsys):
    _ping_file(host, f"healthchecks {receiver.url}/{PING_TOKEN}\n")

    out, state = _check(host, capsys, "--dry-run")

    assert "healthchecks ping (ok) to 127.0.0.1: not sent (dry run)" in out
    assert receiver.received == []
    assert state == {}


def test_the_readiness_probe_asks_with_the_health_credential_and_names_what_failed(receiver):
    module = _load()
    probe = module.READINESS_PROBE.replace("http://127.0.0.1:8420", receiver.url)
    credentials = {
        "client": {"secret": "client-key-0000000000", "scopes": ["submit", "read", "health-detail"]},
        "monitor": {"secret": "monitor-key-000000000", "scopes": ["health-detail"]},
    }
    environment = {**os.environ, "SCRAPEYARD_API_CREDENTIALS": json.dumps(credentials)}

    done = subprocess.run(
        [sys.executable, "-"], input=probe, capture_output=True, text=True, env=environment, check=True
    )

    answer = json.loads(done.stdout)
    assert answer == {
        "code": 503, "asked": "/health/ready", "status": "unhealthy",
        "failing": ["redis", "worker"], "active": 1, "max": 2, "queued": 3,
    }
    [(path, headers, _body)] = receiver.received
    sent = {name.lower(): value for name, value in headers.items()}
    assert path == "/health/ready" and sent["x-api-key"] == "monitor-key-000000000"
