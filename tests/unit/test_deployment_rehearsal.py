from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.rehearsal import verify_deployment
from tests.rehearsal.verify_deployment import KNOWN_CONFIG, PROJECT, RehearsalFailure

RUNNER = Path("scripts/run_deployment_rehearsal.sh")
CLIENT_KEY = "client-secret"
MONITOR_KEY = "monitor-secret"


class _FakeDeployment(ThreadingHTTPServer):
    """The parts of the API the rehearsal reads, with one switchable fault."""

    def __init__(self, fault: str | None = None) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.fault = fault
        self.runs: dict[str, str] = {}
        self.result_status = "failed"
        self.ready_polls = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"


class _Handler(BaseHTTPRequestHandler):
    server: _FakeDeployment

    def log_message(self, *_args: Any) -> None:
        pass

    def _send(self, status: int, body: Any, headers: dict[str, str] | None = None) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def _caller(self) -> str | None:
        return {CLIENT_KEY: "client", MONITOR_KEY: "monitor"}.get(
            self.headers.get("X-API-Key", "")
        )

    def do_GET(self) -> None:
        fault, caller, path = self.server.fault, self._caller(), self.path.split("?")[0]
        if path == "/health":
            return self._send(200, {"status": "ok"})
        if path == "/health/ready":
            if caller is None and fault != "readiness_is_public":
                return self._send(401, {"error": "unauthenticated"})
            if caller == "client" and fault != "client_reads_readiness":
                return self._send(403, {"error": "forbidden"})
            self.server.ready_polls += 1
            if fault == "never_ready" or self.server.ready_polls < 2:
                return self._send(503, {"status": "unhealthy"})
            return self._send(200, {"status": "ok", "workers": {"active_tasks": 0}})
        if caller != "client":
            return self._send(401, {"error": "unauthenticated"})
        parts = path.strip("/").split("/")
        if parts[0] == "jobs" and len(parts) == 4:
            return self._send(200, {"job_id": parts[1], "run_id": parts[3], "status": "failed"})
        if parts[0] == "results":
            run_id = self.server.runs[IDEMPOTENCY_KEY]
            return self._send(
                200,
                {
                    "job_id": parts[1],
                    "run_id": run_id,
                    "status": self.server.result_status,
                    "project": PROJECT,
                    "name": "known-run",
                    "config_hash": "0" * 64,
                    "completed_at": "2026-10-10T00:00:00Z",
                    "added_by_a_later_release": True,
                },
            )
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if self._caller() != "client":
            return self._send(403 if self._caller() else 401, {"error": "forbidden"})
        assert self.headers["Content-Type"] == "application/x-yaml"
        assert body == KNOWN_CONFIG.encode()
        key = self.headers["Idempotency-Key"]
        replayed = key in self.server.runs and self.server.fault != "replay_starts_a_run"
        if not replayed:
            self.server.runs[key] = f"run-{len(self.server.runs)}-{os.urandom(2).hex()}"
        self._send(
            202,
            {"job_id": "job-1", "run_id": self.server.runs[key], "status": "queued"},
            {"Idempotency-Replayed": "true"} if replayed else None,
        )


IDEMPOTENCY_KEY = verify_deployment.IDEMPOTENCY_KEY


@pytest.fixture
def deployment(request: pytest.FixtureRequest):
    server = _FakeDeployment(getattr(request, "param", None))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_verifier_records_a_known_run_and_finds_it_unchanged(deployment, tmp_path, monkeypatch):
    monkeypatch.setattr(verify_deployment.time, "sleep", lambda _seconds: None)
    known_path = tmp_path / "known.json"
    known = verify_deployment.record(deployment.url, CLIENT_KEY, known_path, timeout=5)
    # Only the fields Eyebox depends on are kept, so a newer release may add more.
    assert set(known) == set(verify_deployment.KNOWN_FIELDS)
    assert json.loads(known_path.read_text()) == known
    report = verify_deployment.check(deployment.url, CLIENT_KEY, MONITOR_KEY, known_path)
    assert report["known_run"] == known
    assert report["readiness"] == "ok"
    assert len(deployment.runs) == 1

    deployment.result_status = "complete"
    with pytest.raises(RehearsalFailure, match="known run changed"):
        verify_deployment.check(deployment.url, CLIENT_KEY, MONITOR_KEY, known_path)


@pytest.mark.parametrize(
    ("deployment", "message"),
    [
        ("readiness_is_public", "/health/ready without a credential"),
        ("client_reads_readiness", "readiness with the client credential"),
        ("never_ready", "readiness: HTTP 503"),
        ("replay_starts_a_run", "submission was not replayed"),
    ],
    indirect=["deployment"],
)
def test_verifier_refuses_a_deployment_that_breaks_a_contract(
    deployment, message, tmp_path, monkeypatch
):
    monkeypatch.setattr(verify_deployment.time, "sleep", lambda _seconds: None)
    known_path = tmp_path / "known.json"
    verify_deployment.record(deployment.url, CLIENT_KEY, known_path, timeout=5)
    with pytest.raises(RehearsalFailure, match=message):
        verify_deployment.check(
            deployment.url, CLIENT_KEY, MONITOR_KEY, known_path, ready_timeout=0.2
        )


def test_known_config_is_refused_before_any_connection():
    config = yaml.safe_load(KNOWN_CONFIG)
    # RFC 6761 reserves .invalid, so no resolver or proxy can ever answer for it.
    assert config["target"]["url"] == "http://scrapeyard-rehearsal.invalid/"
    assert config["target"]["fetcher"] == "basic"
    assert config["project"] == PROJECT


def test_runner_rehearses_the_production_path_and_cleans_up_after_itself():
    subprocess.run([str(RUNNER), "--help"], check=True, capture_output=True, text=True)
    text = RUNNER.read_text(encoding="utf-8")
    for contract in (
        # Each release is deployed by the wrapper it retains, not by this checkout's.
        '"$release/source/security/deploy-secure-compose.sh" --release "$release"',
        "trap on_exit EXIT",
        "trap 'exit 130' INT TERM HUP",
        "assert_no_project_resources",
        "scan_diagnostics",
        'ip route add unreachable "$PROXY_ADDRESS/32"',
        'ip route del unreachable "$PROXY_ADDRESS/32"',
        "SCRAPEYARD_EGRESS_ALLOW_CIDRS=not-a-network",
        "controlled non-public challenge was reachable",
        "use an isolated Docker host",
        "an image was pulled during the rehearsal",
        'python3 scripts/release_artifact.py verify "$release"',
    ):
        assert contract in text
    for forbidden in (
        "docker compose build",
        "docker build",
        "docker pull",
        "docker system prune",
        "docker builder prune",
        "volume prune",
        "network prune",
        # Production runs untrusted mode behind the probe; no overlay may relax it.
        "docker-compose.smoke.yml",
        "docker-compose.qualification.yml",
        "SCRAPEYARD_UNTRUSTED_SUBMISSIONS",
    ):
        assert forbidden not in text
    # The steps run in the order an operator would meet them.
    steps = [
        text.index(marker)
        for marker in (
            "\ndeploy candidate ",
            "if deploy corrupted ",
            "if deploy policy-failure ",
            "\nremove_egress_policy\n",
            "if deploy uncovered-bridge ",
            "\ndeploy rollback ",
            "\ndeploy roll-forward ",
            'systemctl enable --now "$UNIT"',
            "if promote promote-corrupted ",
            "if promote promote-foreign-owner ",
            "if promote promote-untrusted-directory ",
            "promote promote-interrupted ",
            "promote promote-interrupted-late ",
            "promote promote-during-start ",
            "systemctl restart docker.service",
            "systemctl kill --signal=KILL --kill-whom=main docker.service",
            "await_unit cold-start ",
            "await_unit network-recreated ",
            "the boot service did not recover after its setting was repaired",
            "promote promote-rollback ",
            "await_unit rollback-boot ",
        )
    ]
    assert steps == sorted(steps)


def test_boot_service_phase_follows_the_deployment_guide_and_restores_the_host():
    text = RUNNER.read_text(encoding="utf-8")
    for contract in (
        # The unit and the script are the candidate's, installed as documented.
        'install -m 644 "$BOOT_CANDIDATE/source/security/systemd/$UNIT" "$UNIT_FILE"',
        'install -m 755 "$BOOT_CANDIDATE/source/security/manage-release.sh" "$MANAGE"',
        'MANAGE="/usr/local/sbin/scrapeyard-release"',
        'ln -s -- "$BOOT_CANDIDATE" "$RELEASES_ROOT/current"',
        # A release is refused unless root owns it and every directory above
        # it, as after extraction by root under a root-owned directory.
        'install -d -m 755 -o 0 -g 0 "$RELEASES_ROOT"',
        'chown -R 0:0 "$BOOT_CANDIDATE" "$BOOT_PREVIOUS"',
        'chown -R "$CANDIDATE_OWNER" "$CANDIDATE"',
        'chown -R "$PREVIOUS_OWNER" "$PREVIOUS"',
        # A start that waited for a promotion is checked against what was promoted.
        'verify_runtime start-after-promotion "$BOOT_PREVIOUS" previous',
        # The bridge hook goes back to what it was, even after a failure.
        'echo "$BRIDGE_HOOK_BEFORE" > /proc/sys/net/bridge/bridge-nf-call-iptables',
        # A reboot loses the policy, the bridge hook and the AppArmor profile,
        # and only Docker's own start brings the boot service back.
        "modprobe -r br_netfilter",
        "echo 0 > /proc/sys/net/bridge/bridge-nf-call-iptables",
        'install-chromium-apparmor-profile.sh" remove',
        "systemctl stop docker.socket docker.service\n",
        "  systemctl start docker.service\n  await_unit cold-start",
        # Only the pauses between attempts are shortened.
        "[Service]\\nRestartSec=3\\nRestartMaxDelaySec=6\\n",
        # Cleanup undoes what the phase did, Docker's stop included.
        'systemctl stop "$PROMOTE_UNIT"',
        'systemctl disable --now "$UNIT"',
        'rm -rf -- "$UNIT_FILE" "$UNIT_DROP_IN" "$ENV_DIR" "$MANAGE" "$RELEASES_ROOT/current"',
        'rm -rf -- "$BOOT_CANDIDATE"',
        "systemctl start docker.service >/dev/null 2>&1",
        "already exists; use an isolated host",
        "the AppArmor profile is already loaded; use an isolated host",
    ):
        assert contract in text, contract
    # A promotion is never cut short by a timeout of the rehearsal's own: the
    # deployment would go on in its unit with nobody waiting for it.
    promote = text[text.index("  promote() {") : text.index("  echo \"Installing the boot service")]
    assert "timeout" not in promote


@pytest.mark.skipif(os.geteuid() == 0, reason="the refusal is for an unprivileged caller")
def test_runner_refuses_to_run_without_root(tmp_path):
    result = subprocess.run(
        [str(RUNNER), "--candidate", str(tmp_path / "a"), "--previous", str(tmp_path / "b")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "run as root" in result.stderr


def test_rehearsal_workflow_builds_both_releases_and_hands_over_only_a_dispatched_one():
    workflow = yaml.load(
        Path(".github/workflows/release-rehearsal.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    assert set(workflow["on"]) == {"pull_request", "workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    assert "github.event_name == 'pull_request'" in workflow["concurrency"]["cancel-in-progress"]
    paths = set(workflow["on"]["pull_request"]["paths"])
    assert {
        "docker-compose.yml",
        "security/**",
        "scripts/release_artifact.py",
        "scripts/run_deployment_rehearsal.sh",
    } <= paths
    job = workflow["jobs"]["release-rehearsal"]
    assert job["runs-on"] == "ubuntu-24.04"
    steps = job["steps"]
    assert steps[0]["with"]["fetch-depth"] == "0"
    assert not any(step.get("uses", "").startswith("actions/cache@") for step in steps)
    commands = [step.get("run", "") for step in steps]
    created = [command for command in commands if "release_artifact.py create" in command]
    assert len(created) == 2
    rehearsal = next(i for i, c in enumerate(commands) if "run_deployment_rehearsal.sh" in c)
    assert all(commands.index(command) < rehearsal for command in created)
    assert commands[rehearsal].startswith("sudo env ")
    assert "run_deployment_rehearsal.sh --boot-service" in commands[rehearsal]
    uploads = [step for step in steps if step.get("uses", "").startswith("actions/upload-artifact@")]
    evidence, release = uploads
    assert evidence["if"] == "always()"
    assert release["if"] == "github.event_name == 'workflow_dispatch'"
    assert release["with"]["if-no-files-found"] == "error"
    pack = next(step for step in steps if step.get("name", "").startswith("Pack the rehearsed"))
    # Every run packs the release and checks what an operator would extract.
    assert "if" not in pack
    assert "sha256sum" in pack["run"]
    # Root runs a release, so neither the archive nor its extraction may keep
    # the runner's account as the owner.
    assert "--owner=0 --group=0 --numeric-owner" in pack["run"]
    assert "--no-same-owner" in pack["run"]
    assert "release_artifact.py verify" in pack["run"]
    assert "! -user 0 -o -perm /022" in pack["run"]
