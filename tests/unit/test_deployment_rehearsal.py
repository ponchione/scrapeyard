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

from tests.rehearsal import probe_health, verify_deployment
from tests.rehearsal.verify_deployment import KNOWN_CONFIG, PROJECT, RehearsalFailure

RUNNER = Path("scripts/run_deployment_rehearsal.sh")
CLIENT_KEY = "client-secret"
MONITOR_KEY = "monitor-secret"
SECRET_VALUE = "http://rehearsal:0123456789abcdef@203.66.81.250:3128"


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
        if parts[0] == "jobs" and len(parts) == 2:
            config = verify_deployment.secret_reference_config(verify_deployment.SECRET_NAME)
            if fault == "secret_leaks":
                config = config.replace("${" + verify_deployment.SECRET_NAME + "}", SECRET_VALUE)
            return self._send(200, {"job_id": parts[1], "config_yaml": config})
        if parts[0] == "jobs" and len(parts) == 4:
            status = "queued" if fault == "never_runs" else "failed"
            return self._send(200, {"job_id": parts[1], "run_id": parts[3], "status": status})
        if parts[0] == "results":
            run_id = self.server.runs.get(IDEMPOTENCY_KEY)
            leaked = {"secret_leaks": SECRET_VALUE, "password_leaks": "Proxy password 0123456789abcdef"}
            return self._send(
                200,
                {
                    **({"leaked": leaked[fault]} if fault in leaked else {}),
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
        missing = verify_deployment.secret_reference_config(verify_deployment.MISSING_SECRET_NAME)
        defined = verify_deployment.secret_reference_config(verify_deployment.SECRET_NAME)
        assert body in {KNOWN_CONFIG.encode(), missing.encode(), defined.encode()}
        unresolved = {missing.encode(): verify_deployment.MISSING_SECRET_NAME}
        if self.server.fault == "secret_unresolved":
            unresolved[defined.encode()] = verify_deployment.SECRET_NAME
        if body in unresolved and self.server.fault != "missing_secret_accepted":
            return self._send(
                422, {"error": f"Invalid config: Missing deployment secret reference {unresolved[body]}"}
            )
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


def test_verifier_runs_new_work_under_a_key_never_used_before(deployment, tmp_path, monkeypatch):
    monkeypatch.setattr(verify_deployment.time, "sleep", lambda _seconds: None)
    known = verify_deployment.record(deployment.url, CLIENT_KEY, tmp_path / "known.json", timeout=5)

    run = verify_deployment.run_new(deployment.url, CLIENT_KEY, timeout=5)

    assert run["status"] == "failed"
    assert run["run_id"] != known["run_id"]
    assert len(deployment.runs) == 2


def test_verifier_runs_a_job_that_names_a_deployment_secret(deployment, monkeypatch):
    monkeypatch.setattr(verify_deployment.time, "sleep", lambda _seconds: None)

    report = verify_deployment.secret_reference(deployment.url, CLIENT_KEY, SECRET_VALUE, timeout=5)

    assert report["undefined_secret_refused"] == 422
    assert report["run"]["status"] == "failed"
    assert len(deployment.runs) == 1


@pytest.mark.parametrize(
    ("deployment", "message"),
    [
        # Compose did not hand the secret to the application.
        ("secret_unresolved", "Missing deployment secret reference SCRAPEYARD_SECRET_REHEARSAL_PROXY"),
        # The control proves nothing if an undefined name is accepted too.
        ("missing_secret_accepted", "a job naming an undefined secret"),
        ("secret_leaks", "appeared in the result"),
        ("password_leaks", "appeared in the result"),
    ],
    indirect=["deployment"],
)
def test_verifier_refuses_a_secret_that_is_not_resolved_or_shows(deployment, message, monkeypatch):
    monkeypatch.setattr(verify_deployment.time, "sleep", lambda _seconds: None)
    with pytest.raises(RehearsalFailure, match=message):
        verify_deployment.secret_reference(deployment.url, CLIENT_KEY, SECRET_VALUE, timeout=5)


@pytest.mark.parametrize("deployment", ["never_runs"], indirect=True)
def test_verifier_refuses_new_work_that_never_runs(deployment, monkeypatch):
    clock = iter(range(0, 1000, 1))
    monkeypatch.setattr(verify_deployment.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(verify_deployment.time, "monotonic", lambda: next(clock))

    with pytest.raises(RehearsalFailure, match="did not finish within 5s"):
        verify_deployment.run_new(deployment.url, CLIENT_KEY, timeout=5)


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
        # The host's own addresses are probed after every deployment of a
        # release whose installer covers them, and the listener is cleaned up.
        'if app_connects "$address" "$HOST_PROBE_PORT"; then',
        "the application reached the host's own address",
        'kill "$HOST_LISTENER_PID"',
        "host_policy_jumps",
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
        # Production runs behind the probe; no overlay may relax that.
        "docker-compose.smoke.yml",
        "docker-compose.qualification.yml",
    ):
        assert forbidden not in text
    # Everything before the boot service runs Compose's default, untrusted
    # submissions behind a proxy. Only the settings file of the last steps
    # switches to first-party submissions without one.
    base_phase = text[: text.index("  write_deploy_env() {")]
    assert "SCRAPEYARD_UNTRUSTED_SUBMISSIONS" not in base_phase
    # The steps run in the order an operator would meet them.
    steps = [
        text.index(marker)
        for marker in (
            "\ndeploy candidate ",
            "if deploy no-proxy ",
            "if deploy corrupted ",
            "if deploy policy-failure ",
            "\nexpect_refusal_without_policy unprotected-start\n",
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
            "promote promote-no-proxy ",
            "expect_refusal_without_policy unprotected-start-no-proxy",
            "await_unit boot-no-proxy ",
            'if "$BACKUP" run > ',
            'if "$BACKUP" run > "$DIAGNOSTICS_DIR/backup-bad-recipient.log"',
            'if "$BACKUP" run > "$DIAGNOSTICS_DIR/backup-bad-setting.log"',
            'systemctl start "$BACKUP_UNIT.service"',
            "await_unit backup ",
            '"$BACKUP" restore-test "$BACKUP_ARCHIVE"',
            "verify_runtime restore-test ",
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
        # The bridge hook goes back to what it was before the run, read
        # before the first deployment switches it on.
        'BRIDGE_HOOK_BEFORE="$(cat /proc/sys/net/bridge/bridge-nf-call-iptables 2>/dev/null || echo unloaded)"',
        'echo "$BRIDGE_HOOK_BEFORE" > /proc/sys/net/bridge/bridge-nf-call-iptables',
        # The directory check is tested with a copy root owns throughout.
        'copy_release "$BOOT_PREVIOUS" "$UNTRUSTED"',
        "so what it holds cannot be trusted",
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
        # Between deployments: the policy's jumps are taken away under the
        # running application, and only the timer starts the check.
        'install -m 644 "$BOOT_CANDIDATE/source/security/systemd/$WATCH_UNIT" "$WATCH_UNIT_FILE"',
        'done < <(policy_jumps; host_policy_jumps)',
        'systemctl enable --now "$WATCH_TIMER"',
        # The timer's first run finds the policy in place; a later one has to
        # be what restores it, and the application must not have been started
        # again (a restart keeps the container's identifier).
        "the policy's jumps came back without a later run of the timer",
        'WATCHED_STARTED_AT="$(docker inspect --format \'{{.State.StartedAt}}\' "$APP_CONTAINER")"',
        "putting the policy back restarted the application",
        'systemctl disable --now "$WATCH_TIMER" >/dev/null 2>&1\n    systemctl stop "$WATCH_UNIT"',
        'rm -rf -- "$WATCH_UNIT_FILE" "$WATCH_TIMER_FILE" "$(dirname -- "$RESTORED_LOG")"',
        # An application whose worker loses Redis ends, and comes back by
        # Docker's restart alone, ready to run new work (TD-89).
        "the application did not end after its worker lost Redis",
        "the application that lost Redis did not finish its own shutdown",
        "the application came back through the boot service, not by itself",
        "python3 tests/rehearsal/verify_deployment.py run",
        "a job submitted after Redis came back did not run",
        # The host's Host list is set, and a foreign Host gets nothing (TD-90).
        "printf \"SCRAPEYARD_ALLOWED_HOSTS='172.30.0.250,127.0.0.1'\\n\"",
        "the application answered a request for a host it does not name",
        # The last steps run the settings a host without an operator proxy
        # uses: the application's own log must show that it checked the policy.
        "printf \"SCRAPEYARD_UNTRUSTED_SUBMISSIONS='false'\\n\"",
        "grep -q 'Connected-IP egress policy attested'",
    ):
        assert contract in text, contract
    # A promotion is never cut short by a timeout of the rehearsal's own: the
    # deployment would go on in its unit with nobody waiting for it.
    promote = text[text.index("  promote() {") : text.index("  echo \"Installing the boot service")]
    assert "timeout" not in promote


def test_boot_service_phase_backs_up_through_the_unit_and_restores_in_isolation():
    text = RUNNER.read_text(encoding="utf-8")
    step = text[
        text.index("# ---- Scheduled backup and isolated restore test") : text.index(
            "# ---- end of the backup step"
        )
    ]
    for contract in (
        # The script and both units are the candidate's, installed as documented.
        'install -m 755 "$BOOT_CANDIDATE/source/security/backup.sh" "$BACKUP"',
        '"$BOOT_CANDIDATE/source/security/systemd/$BACKUP_UNIT.timer" /etc/systemd/system/',
        'systemctl enable --now "$BACKUP_UNIT.timer"',
        # The host is given the public half of the key and nothing else.
        'age-keygen -y "$WORK_DIR/backup-identity" > "$ENV_DIR/backup-recipients"',
        # A hook root does not own is refused, with the application untouched.
        'chown 65534:65534 "$ENV_DIR/backup-upload"',
        "a refused backup disturbed the running application",
        # A recipient age does not accept, and a settings line the unit's start
        # could not read, are both refused before anything is stopped, and the
        # settings file is put back before the backup that must succeed.
        'echo "age1notarecipient" >> "$ENV_DIR/backup-recipients"',
        'install -m 644 "$WORK_DIR/backup-recipients" "$ENV_DIR/backup-recipients"',
        'write_deploy_env "SCRAPEYARD_LOG_LEVEL=INFO"',
        "grep -q -F 'could not be started again'",
        "    write_deploy_env\n",
        # The backup runs as the timer would start it, and the application it
        # stopped is back under a new invocation of the boot service, with
        # the images, confinement and egress policy of any other start.
        'systemctl start "$BACKUP_UNIT.service" || BACKUP_OUTCOME=$?',
        'await_unit backup "$BEFORE"',
        'verify_runtime backup "$BOOT_CANDIDATE" candidate',
        'verify_direct_egress backup "$BACKUP_STARTED_EPOCH"',
        'verify_api backup --known "$WORK_DIR/known-run.json"',
        # What was written is an age file, alone with its checksum, and the
        # hook was handed that file.
        '[[ "$(head -c 21 "$BACKUP_ARCHIVE")" == "age-encryption.org/v1" ]]',
        'cmp -s "$BACKUP_ARCHIVE" "$WORK_DIR/uploaded-backup"',
        'status["uploaded"] is True and status["application_down_seconds"] > 0',
        # The restore test is given the private half, proves what it read,
        # leaves nothing, and leaves the deployment as it was.
        '"$BACKUP" restore-test "$BACKUP_ARCHIVE" --identity "$WORK_DIR/backup-identity"',
        "'encryption envelopes under key rehearsal'",
        # The kernel's label of the copy, and a bridge with no address of the host's.
        "'confined as scrapeyard-chromium (enforce)'",
        '"no address of the host\'s on its bridge"',
        "newest failed job: [0-9a-f-]{8,}",
        '[[ -z "$(restore_test_resources)" ]]',
        "the restore test disturbed the running application",
        'verify_runtime restore-test "$BOOT_CANDIDATE" candidate',
    ):
        assert contract in step, contract
    # Nothing in the step but the backup itself stops or starts the
    # application, so what is verified afterwards is the backup's own restart.
    for forbidden in ("promote ", "deploy ", "docker stop", "docker start", "systemctl restart", '"$UNIT"'):
        assert forbidden not in step, forbidden
    # Before anything is deployed, a host that already has a backup installed
    # is refused, and afterwards everything the step installed is removed.
    for contract in (
        'BACKUP="/usr/local/sbin/scrapeyard-backup"',
        '"/etc/systemd/system/$BACKUP_UNIT.timer" "$BACKUP_DIR" "$BACKUP_STATUS_DIR"; do',
        'systemctl disable --now "$BACKUP_UNIT.timer"',
        'rm -rf -- "$BACKUP" "/etc/systemd/system/$BACKUP_UNIT.service"',
        'label=com.docker.compose.project=$PROJECT-restore-test" | \\\n    xargs -r docker volume rm',
    ):
        assert contract in text, contract
    cleanup = text[text.index("cleanup_resources() {") : text.index("on_exit() {")]
    # A backup still running would start the boot service again after it was disabled.
    assert cleanup.index('systemctl stop "$BACKUP_UNIT.service"') < cleanup.index(
        'systemctl disable --now "$UNIT"'
    )


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
    # The backup step encrypts with age, from the distribution's own package
    # as on a host, not from a download.
    installs = [i for i, c in enumerate(commands) if "apt-get install" in c and " age" in c]
    assert installs and installs[0] < rehearsal
    assert not any("github.com/FiloSottile/age" in command for command in commands)
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


def _health_log(*seconds: float, exit_code: int = 0) -> list[dict[str, Any]]:
    return [
        {"Start": "2026-10-11T10:51:41.5Z", "End": f"2026-10-11T10:51:{41.5 + taken:012.9f}Z", "ExitCode": exit_code}
        for taken in seconds
    ]


def test_probe_health_compares_the_fastest_check_that_passed():
    # Docker trims a timestamp's trailing zeros and may write a zone offset.
    assert probe_health.fastest_seconds(_health_log(0.2, 2.4, 0.31)) == pytest.approx(0.2)
    offset = [
        {"Start": "2026-10-11T03:51:59.9-07:00", "End": "2026-10-11T03:52:00.150000001-07:00", "ExitCode": 0}
    ]
    assert probe_health.fastest_seconds(offset) == pytest.approx(0.25, abs=1e-6)
    # A check refused before the server was up is quick and proves nothing.
    refused_then_slow = _health_log(0.04, exit_code=1) + _health_log(2.8)
    assert probe_health.fastest_seconds(refused_then_slow) == pytest.approx(2.8)

    backwards = [{"Start": "2026-11-01T01:59:59.5-07:00", "End": "2026-11-01T01:00:00.1-08:00", "ExitCode": 0}]
    for log, message in (
        ([], "no health check that passed"),
        (_health_log(0.04, exit_code=1), "no health check that passed"),
        ([{"Start": "soon", "End": "later", "ExitCode": 0}], "not a Docker timestamp"),
        (backwards, "the clock was set back"),
    ):
        with pytest.raises(ValueError, match=message):
            probe_health.fastest_seconds(log)


@pytest.mark.parametrize(("seconds", "status"), [((0.2, 0.21, 2.9), 0), ((2.4, 2.6, 2.5), 1)])
def test_probe_health_fails_a_check_that_is_slow_every_time(seconds, status):
    done = subprocess.run(
        ["python3", "tests/rehearsal/probe_health.py"],
        input=json.dumps(_health_log(0.03, exit_code=1) + _health_log(*seconds)),
        capture_output=True,
        text=True,
    )

    assert done.returncode == status
    assert done.stdout.strip() == f"{min(seconds):.3f}"
    assert ("over the 0.5s this runner allows" in done.stderr) == bool(status)


def test_runner_times_the_probe_health_check_wherever_it_verifies_a_deployment():
    text = RUNNER.read_text(encoding="utf-8")
    verification = text[text.index("verify_runtime() {") : text.index("\n}\n", text.index("verify_runtime() {"))]

    assert (
        'if [[ "$name" == candidate ]]; then\n'
        "    docker inspect --format '{{json .State.Health.Log}}' \"$(service_containers egress-probe)\" | \\\n"
        "      python3 tests/rehearsal/probe_health.py"
    ) in verification
    assert "the probe's health check is too slow for a slower host" in verification
