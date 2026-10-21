"""The host check (security/health-check.py): what it finds and what it sends."""

from __future__ import annotations

import datetime
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path("security/health-check.py")
SETTING_SECRET = "settings-secret-0123456789"
PING_TOKEN = "ping-token-4f1e9c"
ACCESS_SECRET = "access-secret-7d2a91"


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
        moved = self.path.startswith("/moved")
        self.send_response(302 if moved else 200)
        if moved:
            self.send_header("Location", "/elsewhere")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        self.server.received.append((self.path, dict(self.headers), ""))
        if self.path.startswith("/edge"):
            # The public /health: ok only for the right Access token.
            granted = self.headers.get("CF-Access-Client-Secret") == ACCESS_SECRET
            code = int(self.path.rpartition("-")[2]) if granted and "-" in self.path else 200 if granted else 401
            body = json.dumps({"status": "ok"}).encode() if code == 200 else b""
            self.send_response(code)
            if code == 302:
                self.send_header("Location", "/login")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # The application's readiness, for the in-container probe.
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
        # systemctl show answers: the boot unit has started, and nothing else.
        self.shown: dict[tuple[str, str], str] = {
            ("scrapeyard.service", "SubState"): "exited",
            ("cloudflared-scrapeyard.service", "LoadState"): "loaded",
        }
        # Timers and long-running units are active; the backup and the
        # promotion run now and then.
        self.unit_states: dict[str, str] = {
            "scrapeyard-backup.service": "inactive",
            "scrapeyard-promote.service": "inactive",
        }

    def backup(self, *, hours_ago: float) -> None:
        completed = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours_ago)
        self.module.BACKUP_STATUS.write_text(json.dumps({
            "completed_at": completed.strftime("%Y-%m-%dT%H:%M:%SZ"), "bytes": 1849686, "uploaded": False,
        }))

    def run(self, command: list[str], *, stdin: str | None = None) -> tuple[int, str]:
        if command[0] == "docker":
            assert self.unit_states.get("docker.service", "active") == "active"
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
            prop = command[2].removeprefix("--property=")
            return 0, self.shown.get((command[4], prop), "0") + "\n"
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
        "EDGE_ACCESS_FILE": root / "etc/scrapeyard/edge-access.json",
        "STATE_FILE": root / "var/lib/scrapeyard/health-status.json",
        "BACKUP_STATUS": root / "var/lib/scrapeyard/backup-status.json",
        "POLICY_RECORD": root / "var/lib/scrapeyard/policy-restored.log",
        "BACKUP_STOP_MARKER": root / "run-backup-stopped-application",
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
    assert receiver.received[0][2].startswith("Scrapeyard host: OK")
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
        # Made while the clock was ahead: it read as fresh for the 10 days.
        (-240, "active", "FAIL backup: last completed 240.0 h in the future; timer active"),
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


@pytest.mark.parametrize("docker", ["inactive", "failed", "deactivating"])
def test_a_stopped_docker_is_not_asked_so_it_does_not_start_again(host, capsys, docker):
    # Its socket would start it for the first client, and the application with it.
    host.unit_states["docker.service"] = docker

    out, state = _check(host, capsys)

    assert (
        f"FAIL deployment: its containers could not be found: docker.service is {docker}, "
        "so Docker was not asked"
    ) in out
    assert "FAIL application: no application container" in out
    assert state["status"] == "fail"


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
    assert lines[0] == "ok changed" and lines[1].startswith("Scrapeyard host: OK")
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


def test_the_first_policy_record_ever_written_is_reported(host, capsys):
    out, _state = _check(host, capsys)
    assert "ok   policy-watch: timer active; nothing recorded" in out

    host.module.POLICY_RECORD.write_text("2020-01-01T00:00:00Z NOT PUT BACK: INPUT has no jump\n")
    out, _state = _check(host, capsys)

    assert "FAIL policy-watch: timer active; 1 new record line(s): 2020-01-01T00:00:00Z NOT PUT BACK" in out


def test_without_a_status_file_only_recent_record_lines_are_new(host, capsys):
    now = datetime.datetime.now(datetime.timezone.utc)
    lines = [
        (now - datetime.timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ") + " INPUT has no jump",
        (now - datetime.timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ") + " DOCKER-USER has no jump",
    ]
    host.module.POLICY_RECORD.write_text("\n".join(lines) + "\n")

    out, _state = _check(host, capsys)

    assert "FAIL policy-watch: timer active; 1 new record line(s): " + lines[1] in out


# A runner may have booted less than a few minutes ago, so the check is given
# a monotonic clock far enough from boot for any test's past.
MONOTONIC_NOW = 1_000_000.0


def _monotonic_us(seconds_ago: float) -> str:
    return str(int((MONOTONIC_NOW - seconds_ago) * 1e6))


def _stopped_by_backup(seconds_ago: float):
    def setup(host):
        host.module.BACKUP_STOP_MARKER.touch()
        os.utime(host.module.BACKUP_STOP_MARKER, (time.time() - seconds_ago,) * 2)
    return setup


@pytest.mark.parametrize(
    ("setup", "overdue"),
    [
        # A backup that stopped it a minute ago.
        (_stopped_by_backup(60), None),
        # The 2026-10-21 outage as the 20:40:24 UTC run saw it: a backup had
        # had it stopped for 266 s. That run said "not judged" and pinged
        # success; it must fail.
        (_stopped_by_backup(266), "a backup has it stopped for 266s"),
        (_stopped_by_backup(900), "a backup has it stopped for 900s"),
        # The boot unit's start under way.
        (lambda host: host.shown.update({("scrapeyard.service", "SubState"): "start",
                                          ("scrapeyard.service", "StateChangeTimestampMonotonic"): _monotonic_us(60)}), None),
        # Between retries of a start that keeps failing: not maintenance at all.
        (lambda host: host.shown.update({("scrapeyard.service", "SubState"): "auto-restart",
                                          ("scrapeyard.service", "StateChangeTimestampMonotonic"): _monotonic_us(60)}), ""),
        # A start that has hung past the window.
        (lambda host: host.shown.update({("scrapeyard.service", "SubState"): "start",
                                          ("scrapeyard.service", "StateChangeTimestampMonotonic"): _monotonic_us(200)}),
         "scrapeyard.service is starting or stopping it for 200s"),
        # A promotion under way.
        (lambda host: (host.unit_states.update({"scrapeyard-promote.service": "active"}),
                       host.shown.update({("scrapeyard-promote.service", "StateChangeTimestampMonotonic"): _monotonic_us(30)})), None),
        (lambda host: (host.unit_states.update({"scrapeyard-promote.service": "active"}),
                       host.shown.update({("scrapeyard-promote.service", "StateChangeTimestampMonotonic"): _monotonic_us(400)})),
         "a promotion is under way for 400s"),
    ],
)
def test_maintenance_is_not_judged_for_three_minutes_and_then_fails_as_overdue(
    host, capsys, monkeypatch, setup, overdue
):
    monkeypatch.setattr(host.module, "monotonic", lambda: MONOTONIC_NOW)
    host.ready = {"code": 503, "asked": "/health/ready", "status": "unhealthy", "failing": ["redis"]}
    setup(host)

    out, state = _check(host, capsys)

    if overdue is None:
        assert "ok   application: not judged: " in out and state["status"] == "ok"
    else:
        line = next(line for line in out.splitlines() if "application:" in line)
        assert line.startswith("FAIL application: ") and "not ready: HTTP 503" in line
        assert state["status"] == "fail"
        if overdue:
            assert f"maintenance overdue: {overdue}, past the 180s allowed" in line


def test_a_stale_backup_marker_does_not_fail_an_application_that_is_up(host, capsys):
    _stopped_by_backup(3 * 86400)(host)

    out, state = _check(host, capsys)

    assert "ok   application: ok" in out and "overdue" not in out and state["status"] == "ok"


def test_ping_urls_must_be_https_and_plain(host, receiver, capsys, monkeypatch):
    monkeypatch.setattr(host.module, "PING_SCHEMES", frozenset({"https"}))
    for line in (
        f"healthchecks http://hc-ping.example/{PING_TOKEN}",
        f"healthchecks https://hc-ping.example/{PING_TOKEN} # production",
        f"ntfy https://user:{PING_TOKEN}@ntfy.example/topic",
        f"ntfy https://[ntfy.example/{PING_TOKEN}",
    ):
        _ping_file(host, line + "\n")

        out, _state = _check(host, capsys)

        assert "FAIL hooks:" in out and "nothing is sent through it" in out
        assert PING_TOKEN not in out


def test_a_failed_or_redirected_send_is_said_by_kind_and_never_followed(host, receiver, capsys):
    _ping_file(
        host,
        # TLS to a server that speaks plain HTTP: refused at once.
        f"healthchecks {receiver.url.replace('http:', 'https:')}/{PING_TOKEN}\n"
        f"ntfy {receiver.url}/moved-{PING_TOKEN}\n",
    )

    out, state = _check(host, capsys)

    assert "healthchecks ping (ok) to 127.0.0.1: URLError" in out
    assert "ntfy message (ok) to 127.0.0.1: HTTP 302" in out
    assert [path for path, _headers, _body in receiver.received if path.startswith("/moved")] == [
        f"/moved-{PING_TOKEN}"
    ]
    assert PING_TOKEN not in out + json.dumps(state)
    # Not delivered, so it is tried again next time.
    _check(host, capsys)
    assert len([path for path, _headers, _body in receiver.received if path.startswith("/moved")]) == 2


def test_each_ntfy_topic_is_told(host, receiver, capsys):
    _ping_file(host, f"ntfy {receiver.url}/topic-a\nntfy {receiver.url}/topic-b\n")

    _check(host, capsys)
    _check(host, capsys)

    assert [path for path, _headers, _body in receiver.received] == ["/topic-a", "/topic-b"]


@pytest.mark.parametrize("problem", ["writable directory", "link"])
def test_a_ping_file_others_could_replace_is_not_used(host, receiver, capsys, problem):
    if problem == "link":
        target = host.root / "etc/scrapeyard/elsewhere"
        target.write_text(f"healthchecks {receiver.url}/{PING_TOKEN}\n")
        target.chmod(0o600)
        host.module.PING_FILE.symlink_to(target)
    else:
        _ping_file(host, f"healthchecks {receiver.url}/{PING_TOKEN}\n")
        (host.root / "etc").chmod(0o775)

    out, _state = _check(host, capsys)

    assert "FAIL hooks:" in out
    assert receiver.received == []


def test_a_run_that_crashes_says_only_what_kind_and_the_next_run_reports_it(host, capsys, monkeypatch):
    original = host.module.check_all

    def crash(*_args, **_kwargs):
        raise RuntimeError(f"https://hc-ping.example/{PING_TOKEN}")

    monkeypatch.setattr(host.module, "check_all", crash)
    assert host.module.main([]) == 1
    out = capsys.readouterr().out
    assert "The check itself failed (RuntimeError)" in out and PING_TOKEN not in out

    monkeypatch.setattr(host.module, "check_all", original)
    out, state = _check(host, capsys)
    assert "FAIL previous-run: ended with RuntimeError before it reported" in out
    out, state = _check(host, capsys)
    assert "previous-run" not in out and state["status"] == "ok"


def test_the_readiness_probe_asks_liveness_of_a_release_without_the_readiness_check(receiver):
    module = _load()
    probe = module.READINESS_PROBE.replace("http://127.0.0.1:8420", receiver.url)

    # -S: no site packages, so no scrapeyard to import, as in an older release.
    done = subprocess.run([sys.executable, "-I", "-S", "-"], input=probe, capture_output=True, text=True, check=True)

    assert json.loads(done.stdout)["asked"] == "/health/live"


def test_a_delivery_that_failed_is_reported_by_the_next_run(host, receiver, capsys):
    _ping_file(host, f"healthchecks {receiver.url}/moved-{PING_TOKEN}\nntfy {receiver.url}/topic\n")

    out, _state = _check(host, capsys)
    assert "ok   hooks: healthchecks, ntfy" in out
    out, _state = _check(host, capsys)

    assert "WARN delivery: last run: healthchecks ping to 127.0.0.1: HTTP 302" in out
    # Said, and not a failure of the host: the state stays as it was.
    assert "OK (9 checks)" in out
    # ntfy heard the first state only: a warning is no change of state.
    assert [path for path, _headers, _body in receiver.received].count("/topic") == 1
    assert PING_TOKEN not in out


def test_the_edge_check_asks_through_access_and_fails_on_anything_but_ok(host, receiver, capsys):
    access = host.module.EDGE_ACCESS_FILE

    out, _state = _check(host, capsys, "--edge-url", f"{receiver.url}/edge")
    assert "FAIL edge:" in out and "edge-access.json" in out
    assert not receiver.received

    access.write_text(json.dumps({"client_id": "monitor.access", "client_secret": ACCESS_SECRET}))
    access.chmod(0o644)
    out, _state = _check(host, capsys, "--edge-url", f"{receiver.url}/edge")
    assert "FAIL edge:" in out and "root's alone" in out and not receiver.received

    access.chmod(0o600)
    out, state = _check(host, capsys, "--edge-url", f"{receiver.url}/edge")
    assert "ok   edge: the public name answered HTTP 200 through Access and the tunnel, status ok" in out
    assert "OK (9 checks)" in out and state["status"] == "ok"
    headers = {name.lower(): value for name, value in receiver.received[-1][1].items()}
    assert headers["cf-access-client-id"] == "monitor.access"
    assert headers["user-agent"] == "scrapeyard-health"

    # The tunnel's 502 while its connector has no connection, Access refusing
    # a token it no longer knows, and a redirect (to a login page) all fail.
    for path, expected in (("/edge-502", "HTTP 502"), ("/edge-302", "HTTP 302")):
        out, state = _check(host, capsys, "--edge-url", f"{receiver.url}{path}")
        assert f"FAIL edge: the public name answered {expected}" in out and state["status"] == "fail"
    access.write_text(json.dumps({"client_id": "monitor.access", "client_secret": "revoked"}))
    out, _state = _check(host, capsys, "--edge-url", f"{receiver.url}/edge")
    assert "FAIL edge: the public name answered HTTP 401" in out
    assert ACCESS_SECRET not in out + host.module.STATE_FILE.read_text()

    # While a backup has the application stopped, the 502 is expected.
    host.module.BACKUP_STOP_MARKER.touch()
    out, _state = _check(host, capsys, "--edge-url", f"{receiver.url}/edge-502")
    assert "ok   edge: not judged: a backup has it stopped" in out


def test_a_credential_fails_the_check_from_thirty_days_before_it_expires(host, capsys):
    today = datetime.datetime.now(datetime.timezone.utc).date()
    later = (today + datetime.timedelta(days=200)).isoformat()
    soon = (today + datetime.timedelta(days=29)).isoformat()
    gone = (today - datetime.timedelta(days=1)).isoformat()

    out, state = _check(host, capsys, "--expires", f"r2-upload={later}", "--expires", f"access-monitor={soon}")
    assert f"FAIL expiry: renew: access-monitor {soon} (in 29 days)" in out
    assert "r2-upload" not in out.split("FAIL expiry:")[1].splitlines()[0]
    assert state["status"] == "fail"

    out, state = _check(host, capsys, "--expires", f"r2-upload={later}")
    assert f"ok   expiry: 1 recorded; the first, r2-upload {later}, in 200 days" in out
    assert state["status"] == "ok"

    out, _state = _check(host, capsys, "--expires", f"r2-upload={gone}", "--expiry-warn-days", "0")
    assert f"FAIL expiry: renew: r2-upload {gone} (expired)" in out

    out, _state = _check(host, capsys, "--expires", "r2-upload=2027/10/21")
    assert "FAIL expiry: --expires 'r2-upload=2027/10/21' is not LABEL=YYYY-MM-DD" in out


def test_a_backup_that_kept_the_application_stopped_too_long_fails_the_check_once(host, capsys):
    # The 2026-10-21 backup: 342.8 s down, ended between two runs.
    status = json.loads(host.module.BACKUP_STATUS.read_text())
    status["application_down_seconds"] = 342.8
    host.module.BACKUP_STATUS.write_text(json.dumps(status))

    out, state = _check(host, capsys)
    assert "FAIL backup: the backup completed at" in out and "stopped 343s, past the 180s allowed (once)" in out
    assert state["status"] == "fail"

    out, state = _check(host, capsys)
    assert "ok   backup: last completed" in out and state["status"] == "ok"

    # A normal stop says nothing.
    status["application_down_seconds"] = 29.0
    status["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    host.module.BACKUP_STATUS.write_text(json.dumps(status))
    out, state = _check(host, capsys)
    assert "ok   backup: last completed" in out
