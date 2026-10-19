from __future__ import annotations

import ipaddress
import json
import re
import socket
import struct
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.smoke import verify_confinement
from tests.smoke.fixture_site import PRIVATE_ORIGIN, PUBLIC_ORIGIN, RebindingResolver, create_server
from tests.smoke.verify_api import (
    PRIVATE_LITERAL_ORIGIN,
    basic_config,
    browser_config,
    unsafe_subrequest_config,
)


def _yaml(path: str) -> dict[str, Any]:
    value = yaml.load(Path(path).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(value, dict)
    return value


def test_compose_memory_admission_leaves_explicit_cgroup_reserve() -> None:
    compose = _yaml("docker-compose.yml")
    service = compose["services"]["scrapeyard"]
    assert service["mem_limit"] == "4g"
    admission_setting = service["environment"]["SCRAPEYARD_WORKERS_MEMORY_LIMIT_MB"]
    assert admission_setting == "${SCRAPEYARD_WORKERS_MEMORY_LIMIT_MB:-3072}"
    admission_mb = 3072
    assert 4096 - admission_mb >= 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def test_smoke_lane_probes_the_host_s_own_addresses_from_the_application() -> None:
    script = Path("scripts/run_container_browser_smoke.sh").read_text(encoding="utf-8")
    # Input to the host is not forwarded traffic, so it needs its own probe:
    # the bridge gateway and the host's network address, from the container.
    assert 'for address in "$BRIDGE_GATEWAY" "$HOST_ADDRESS"; do' in script
    assert "network policy let the application reach the host's own address" in script
    # The listener must be up before a refusal means anything, and gone afterwards.
    assert 'fail "the host probe listener did not start"' in script
    # A container the policy does not name must reach it over the same bridge.
    assert 'fail "the host probe listener is not reachable from the deployment bridge"' in script
    assert script.count('kill "$HOST_LISTENER_PID"') == 2


def test_smoke_lane_checks_the_confinement_against_a_control() -> None:
    script = Path("scripts/run_container_browser_smoke.sh").read_text(encoding="utf-8")
    check = script.index(
        '< tests/smoke/verify_confinement.py > "$DIAGNOSTICS_DIR/confinement.json"'
    )
    control = script.index('--entrypoint python "$IMAGE_ID" - --control', check)
    between = script[check:control]
    # The control container differs from the application's in its AppArmor
    # profile and in nothing else that decides a socket call or a capability.
    for same in (
        "--user 10001:10001",
        "--cap-drop ALL --cap-add SYS_CHROOT",
        "no-new-privileges:true",
        "seccomp=security/seccomp/chromium.json",
        '"apparmor=$CONTROL_PROFILE"',
    ):
        assert same in between
    # Its profile is the application's without the lines under test, and
    # those lines have to be there for the difference to exist.
    assert "-e '/^  deny network \\(alg\\|vsock\\|packet\\),$/d'" in between
    assert "-e '/^  deny capability net_\\(admin\\|raw\\),$/d'" in between
    profile = Path("security/apparmor/scrapeyard-chromium").read_text(encoding="utf-8")
    for line in (
        "deny network alg,",
        "deny network vsock,",
        "deny network packet,",
        "deny capability net_admin,",
        "deny capability net_raw,",
    ):
        assert f"  {line}\n" in profile
    # Loaded under the application's name it would take the place of the
    # application's profile, and it must not outlive the lane.
    assert 'fail "the control profile did not take its own name"' in between
    assert script.count("run_control_profile --remove") == 2
    assert "the control AppArmor profile $CONTROL_PROFILE could not be removed" in script


_CONFINED_ANSWERS: dict[str, dict[str, object]] = {
    "container": {
        "TCP socket, 32-bit entry": "accepted",
        "AF_ALG socket": "EPERM",
        "AF_VSOCK socket": "EPERM",
        "AF_ALG socket, 32-bit entry": "EACCES",
        "AF_VSOCK socket, 32-bit entry": "EACCES",
    },
    "own_namespaces": {
        "own user and network namespace": "accepted",
        "holds CAP_NET_ADMIN and CAP_NET_RAW there": True,
        "packet socket": "EACCES",
        "packet socket, 32-bit entry": "EACCES",
        "raw IP socket": "EPERM",
        "netfilter: create a table": "EPERM",
        "route netlink: create a link": "EPERM",
        "traffic control: add a queueing discipline": "EPERM",
        "raise the loopback interface": "EPERM",
        "route netlink: list the links": "accepted",
        "chroot": "accepted",
    },
}
_UNCONFINED_ANSWERS: dict[str, dict[str, object]] = {
    "container": {
        **_CONFINED_ANSWERS["container"],
        "AF_ALG socket, 32-bit entry": "accepted",
        "AF_VSOCK socket, 32-bit entry": "EAFNOSUPPORT",
    },
    "own_namespaces": {
        name: True if answer is True else "accepted"
        for name, answer in _CONFINED_ANSWERS["own_namespaces"].items()
    },
}


@pytest.mark.parametrize(
    ("arguments", "base", "where", "answers", "status"),
    [
        ([], _CONFINED_ANSWERS, "container", {}, 0),
        # AppArmor let a family through the entry the seccomp filter cannot read.
        ([], _CONFINED_ANSWERS, "container", {"AF_VSOCK socket, 32-bit entry": "accepted"}, 1),
        ([], _CONFINED_ANSWERS, "container", {"AF_ALG socket, 32-bit entry": "EAFNOSUPPORT"}, 1),
        # On a kernel without the 32-bit entry every refusal would be the entry's.
        ([], _CONFINED_ANSWERS, "container", {"TCP socket, 32-bit entry": "ENOSYS"}, 1),
        ([], _CONFINED_ANSWERS, "container", {"an attempt nobody judges": "accepted"}, 1),
        # Each thing a network namespace of the process's own must not give it.
        ([], _CONFINED_ANSWERS, "own_namespaces", {"packet socket": "accepted"}, 1),
        ([], _CONFINED_ANSWERS, "own_namespaces", {"packet socket, 32-bit entry": "accepted"}, 1),
        ([], _CONFINED_ANSWERS, "own_namespaces", {"raw IP socket": "accepted"}, 1),
        ([], _CONFINED_ANSWERS, "own_namespaces", {"netfilter: create a table": "accepted"}, 1),
        ([], _CONFINED_ANSWERS, "own_namespaces", {"route netlink: create a link": "EOPNOTSUPP"}, 1),
        ([], _CONFINED_ANSWERS, "own_namespaces", {"raise the loopback interface": "accepted"}, 1),
        (
            [],
            _CONFINED_ANSWERS,
            "own_namespaces",
            {"traffic control: add a queueing discipline": "accepted"},
            1,
        ),
        # A host that gives the namespace no capability refuses for its own
        # reasons, and one that cannot make the namespace tried nothing.
        (
            [],
            _CONFINED_ANSWERS,
            "own_namespaces",
            {"holds CAP_NET_ADMIN and CAP_NET_RAW there": False},
            1,
        ),
        ([], {**_CONFINED_ANSWERS, "own_namespaces": {}}, "own_namespaces",
         {"own user and network namespace": "EPERM"}, 1),
        # What the browsers' sandboxes need there must not be refused with the rest.
        ([], _CONFINED_ANSWERS, "own_namespaces", {"chroot": "EPERM"}, 1),
        ([], _CONFINED_ANSWERS, "own_namespaces", {"route netlink: list the links": "EPERM"}, 1),
        # Under the control profile a refusal means the check proves nothing.
        (["--control"], _CONFINED_ANSWERS, "container", {}, 1),
        (["--control"], _UNCONFINED_ANSWERS, "container", {}, 0),
        (["--control"], _UNCONFINED_ANSWERS, "own_namespaces", {"raw IP socket": "EPERM"}, 1),
        (["--control"], _UNCONFINED_ANSWERS, "own_namespaces",
         {"route netlink: create a link": "EOPNOTSUPP"}, 0),
        (["--report"], _UNCONFINED_ANSWERS, "container", {}, 0),
    ],
)
def test_confinement_check_judges_what_the_container_answered(
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    base: dict[str, dict[str, object]],
    where: str,
    answers: dict[str, object],
    status: int,
) -> None:
    seen = {**base, where: {**base[where], **answers}}
    monkeypatch.setattr(verify_confinement.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(verify_confinement, "Entry32", lambda: None)
    monkeypatch.setattr(verify_confinement, "in_the_container", lambda _entry: seen["container"])
    monkeypatch.setattr(
        verify_confinement, "in_own_namespaces", lambda _entry: seen["own_namespaces"]
    )

    assert verify_confinement.main(arguments) == status


def test_fixture_serves_deterministic_static_dynamic_redirect_and_protected_scenarios() -> None:
    server = create_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        urllib.request.urlopen(f"{origin}/__reset", timeout=2).read()
        static = urllib.request.urlopen(f"{origin}/static", timeout=2).read().decode()
        dynamic = urllib.request.urlopen(f"{origin}/dynamic", timeout=2).read().decode()
        unsafe = urllib.request.urlopen(f"{origin}/unsafe-subrequest", timeout=2).read().decode()
        assert "static-ok" in static
        for contract in (
            "javascript-ok",
            "consent-ok",
            "scroll-ok",
            "load-more-ok",
            "accept-consent",
            "load-more",
        ):
            assert contract in dynamic
        assert f"{PRIVATE_ORIGIN}/protected?case=browser-subrequest" in unsafe

        opener = urllib.request.build_opener(_NoRedirect)
        for path, expected in (
            ("/redirect-safe", f"{PUBLIC_ORIGIN}/static"),
            ("/redirect-unsafe", f"{PRIVATE_ORIGIN}/protected?case=redirect"),
        ):
            try:
                opener.open(f"{origin}{path}", timeout=2)
            except urllib.error.HTTPError as exc:
                with exc:
                    assert exc.code == 302
                    assert exc.headers["Location"] == expected

        urllib.request.urlopen(f"{origin}/protected?case=unit-proof", timeout=2).read()
        stats = json.loads(urllib.request.urlopen(f"{origin}/__stats", timeout=2).read())
        assert stats["protected_cases"] == {"unit-proof": 1}
        assert stats["protected_total"] == 1
        assert stats["requests"]["/protected"] == 1
        reset = json.loads(urllib.request.urlopen(f"{origin}/__reset", timeout=2).read())
        assert reset["protected_cases"] == {}
        assert reset["protected_total"] == 0
        assert reset["webhook_attempts"] == {}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _dns_query(name: str, record_type: int, identifier: int = 0x1234) -> bytes:
    labels = b"".join(bytes([len(part)]) + part.encode() for part in name.split("."))
    return (
        struct.pack(">HHHHHH", identifier, 0x0100, 1, 0, 0, 0)
        + labels
        + b"\x00"
        + struct.pack(">HH", record_type, 1)
    )


def _dns_answer(response: bytes) -> tuple[int, int, str | None]:
    identifier, flags, _questions, answers = struct.unpack(">HHHH", response[:8])
    address = socket.inet_ntoa(response[-4:]) if answers else None
    return identifier, flags & 0x000F, address


def test_fixture_resolver_rebinds_a_name_after_its_first_address_query() -> None:
    resolver = RebindingResolver("203.66.81.242")

    # Queries for other record types neither answer nor use up the first answer.
    identifier, rcode, address = _dns_answer(resolver.answer(_dns_query("a1.rebind.test", 28, 7)))
    assert (identifier, rcode, address) == (7, 0, None)
    assert _dns_answer(resolver.answer(_dns_query("A1.Rebind.Test", 1)))[1:] == (0, "203.66.81.242")
    assert _dns_answer(resolver.answer(_dns_query("a1.rebind.test", 1)))[1:] == (0, "127.0.0.1")
    assert _dns_answer(resolver.answer(_dns_query("a1.rebind.test", 1)))[1:] == (0, "127.0.0.1")
    # Each name has its own first answer.
    assert _dns_answer(resolver.answer(_dns_query("a2.rebind.test", 1)))[1:] == (0, "203.66.81.242")
    # Anything else does not exist, and a response or a truncated query is ignored.
    assert _dns_answer(resolver.answer(_dns_query("example.com", 1)))[1:] == (3, None)
    assert resolver.answer(_dns_query("a3.rebind.test", 1)[:20]) is None
    assert resolver.queries() == {"a1.rebind.test": 3, "a2.rebind.test": 1}


def _rebinding_payload(status: int | None, excerpt: str = "") -> dict[str, Any]:
    debug = {"main_document_status": status, "html_excerpt": excerpt}
    return {"status": "failed", "targets": [{"status": "failed", "debug": debug}]}


@pytest.mark.parametrize(
    ("outcomes", "error"),
    [
        # Refused by Host on the second try after one fetch that went public.
        ([None, 400], None),
        # The API's own answer, by status or by body, is a failure at once.
        ([200], "answered a name that rebinds"),
        ([_rebinding_payload(None, '<pre>{"status": "ok"}</pre>')], "answered a name that rebinds"),
        # Never reaching the API proves nothing.
        ([None, None, None], "never reached the API"),
    ],
)
def test_rebinding_check_needs_a_refusal_from_the_api_for_each_browser_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcomes: list[Any], error: str | None
) -> None:
    from tests.smoke import verify_api

    hosts: list[str] = []

    def submit(_api: str, _key: str, config: str, *_args: Any) -> dict[str, Any]:
        hosts.append(config.split("url: http://", 1)[1].split(":", 1)[0])
        # Outcomes are per try, and every browser mode starts its tries anew.
        attempt = int(config.split("name: rebinding-", 1)[1].splitlines()[0].rsplit("-", 1)[1])
        outcome = outcomes[attempt - 1]
        return outcome if isinstance(outcome, dict) else _rebinding_payload(outcome)

    monkeypatch.setattr(verify_api, "_submit_and_poll", submit)
    monkeypatch.setattr(verify_api, "_request", lambda *_args, **_kwargs: (200, {}))

    if error:
        with pytest.raises(verify_api.SmokeFailure, match=error):
            verify_api.verify_rebinding_refused("api", "fixture", "key", tmp_path, 5)
        return
    outcome = verify_api.verify_rebinding_refused("api", "fixture", "key", tmp_path, 5)

    assert set(outcome) == {"dynamic", "dynamic-stealth", "stealthy"}
    assert all(tries[-1]["main_document_status"] == 400 for tries in outcome.values())
    assert len(set(hosts)) == len(hosts) == 6
    assert all(host.endswith(".rebind.test") for host in hosts)


def test_smoke_compose_preserves_production_security_and_uses_isolated_ssrf_networks() -> None:
    production = _yaml("docker-compose.yml")
    local = _yaml("docker-compose.local.yml")
    smoke = _yaml("docker-compose.smoke.yml")
    production_app = production["services"]["scrapeyard"]
    egress_probe = production["services"]["egress-probe"]
    smoke_app = smoke["services"]["scrapeyard"]

    assert production_app["build"] == "."
    assert "ports" not in production_app
    assert production_app["read_only"] == "true"
    assert production_app["cap_drop"] == ["ALL"]
    assert production_app["pids_limit"] == "512"
    assert production_app["mem_limit"] == "4g"
    assert production_app["security_opt"] == [
        "no-new-privileges:true",
        "apparmor:scrapeyard-chromium",
        "seccomp:./security/seccomp/chromium.json",
    ]
    assert production_app["cap_add"] == ["SYS_CHROOT"]
    assert production_app["volumes"] == ["scrapeyard-data:/data"]
    assert local["services"]["scrapeyard"]["ports"][0].startswith(
        "${SCRAPEYARD_BIND_ADDRESS:-127.0.0.1}:"
    )
    assert "@sha256:" in production["services"]["redis"]["image"]
    assert production_app["environment"]["SCRAPEYARD_API_CREDENTIALS"].startswith(
        "${SCRAPEYARD_API_CREDENTIALS:?"
    )
    assert production_app["depends_on"]["egress-probe"]["condition"] == "service_healthy"
    assert egress_probe["networks"]["backend"]["ipv4_address"] == "172.30.0.248"
    assert "@sha256:" in egress_probe["image"]
    assert egress_probe["read_only"] == "true"
    assert egress_probe["cap_drop"] == ["ALL"]
    assert "egress_probe.py" in " ".join(egress_probe["command"])
    assert "--liveness-port" in egress_probe["command"]
    assert "--healthcheck" in egress_probe["healthcheck"]["test"]
    assert egress_probe["healthcheck"]["timeout"] == "3s"
    assert smoke_app["environment"]["SCRAPEYARD_BROWSER_DEBUG_ENABLED"] == "true"
    assert smoke_app["environment"]["SCRAPEYARD_UNTRUSTED_SUBMISSIONS"] == "false"
    assert set(smoke_app["depends_on"]) == {"egress-probe", "redis", "fixture"}

    extra_hosts = dict(entry.split("=", 1) for entry in smoke_app["extra_hosts"])
    assert ipaddress.ip_address(extra_hosts["fixture.public.test"]).is_global
    assert ipaddress.ip_address(extra_hosts["fixture.private.test"]).is_private
    assert smoke["networks"]["fixture-public"]["internal"] == "true"
    assert smoke["networks"]["fixture-private"]["internal"] == "true"
    assert "fixture-private" not in smoke_app["networks"]
    assert smoke["services"]["fixture"]["ports"][0].startswith("127.0.0.1:")
    assert smoke_app["ports"][0].startswith("127.0.0.1:")
    assert smoke["services"]["peer"]["networks"] == ["backend"]


def test_smoke_driver_contract_covers_all_modes_actions_and_ssrf_proof() -> None:
    standard = browser_config("dynamic", "dynamic")
    stealth = browser_config("dynamic-stealth", "dynamic", stealth=True)
    stealthy = browser_config("stealthy", "stealthy")
    assert "fetcher: dynamic" in standard
    assert "stealth: false" in standard
    assert "stealth: true" in stealth
    assert "fetcher: stealthy" in stealthy
    for action in ("wait_for_selector", "click", "scroll", "repeat_click"):
        assert f"type: {action}" in standard
    unsafe = unsafe_subrequest_config()
    assert PRIVATE_ORIGIN not in unsafe.split("url:", 1)[1].splitlines()[0]
    assert "disable_resources: false" in unsafe
    assert "#trigger-unsafe" in unsafe
    direct_private = basic_config(
        "direct-private-rejected",
        "/protected",
        private_target=True,
    )
    assert f"url: {PRIVATE_LITERAL_ORIGIN}/protected" in direct_private
    assert PRIVATE_ORIGIN not in direct_private


def test_smoke_runner_has_bounded_cleanup_diagnostics_and_privilege_contracts() -> None:
    runner = Path("scripts/run_container_browser_smoke.sh")
    subprocess.run([str(runner), "--help"], check=True, capture_output=True, text=True)
    text = runner.read_text(encoding="utf-8")
    for contract in (
        "trap on_exit EXIT",
        "down -v --remove-orphans",
        "--filter \"label=com.docker.compose.project=$PROJECT\"",
        "timeout \"$BUILD_TIMEOUT\"",
        "timeout 90 docker compose",
        "timeout 120 docker compose",
        "timeout 60 docker compose",
        "run_egress_policy install",
        "run_egress_policy remove",
        'SCRAPEYARD_EGRESS_INTERFACE="br-${NETWORK_ID:0:12}"',
        "run_apparmor_profile install",
        "run_apparmor_profile remove",
        "fixture.private.test:8080/protected?case=network-layer",
        "169.254.169.254/latest/meta-data",
        # Only a refusal is the policy's; a drop or a lookup failure is not.
        "((refused == 7))",
        "compose exec -T peer",
        "PORT_BINDING",
        "docker stats --no-stream",
        "stop -t 40 scrapeyard",
        "SCRAPEYARD_BIND_ADDRESS=\"127.0.0.1\"",
    ):
        assert contract in text
    assert "docker system prune" not in text
    assert "docker builder prune" not in text
    assert "chown -R root:root /data" not in text


def test_container_browser_workflow_is_path_filtered_least_privilege_and_no_cache() -> None:
    workflow = _yaml(".github/workflows/container-browser-smoke.yml")
    assert set(workflow["on"]) == {"pull_request", "workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] == "true"
    paths = workflow["on"]["pull_request"]["paths"]
    assert "Dockerfile" in paths
    assert "poetry.lock" in paths
    # Every package and migration ships in the image the smoke exercises.
    assert "src/scrapeyard/**" in paths
    assert "sql/**" in paths
    assert "tests/smoke/**" in paths

    job = workflow["jobs"]["production-container-smoke"]
    assert job["timeout-minutes"] == "60"
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert "./scripts/run_container_browser_smoke.sh --no-cache" in commands
    assert not any(
        step.get("uses", "").startswith("actions/cache@")
        for step in job["steps"]
    )
    artifact = next(
        step
        for step in job["steps"]
        if step.get("uses", "").startswith("actions/upload-artifact@")
    )
    assert artifact["if"] == "failure()"
    # Packed first: result artifacts sit under host:port directories, and
    # upload-artifact refuses a path that contains a colon.
    pack = job["steps"][job["steps"].index(artifact) - 1]
    assert pack["if"] == "failure()"
    assert "tar -C artifacts -czf container-browser-smoke-diagnostics.tar.gz" in pack["run"]
    assert artifact["with"]["path"] == "container-browser-smoke-diagnostics.tar.gz"


def test_dockerfile_has_immutable_inputs_and_non_root_runtime_contract() -> None:
    text = Path("Dockerfile").read_text(encoding="utf-8")
    assert "python:3.12.15-slim-bookworm@sha256:" in text
    # A fixed snapshot timestamp, so every build resolves the same packages.
    assert re.search(r"(?m)^ARG DEBIAN_SNAPSHOT=[0-9]{8}T[0-9]{6}Z$", text)
    assert "snapshot.debian.org/archive/debian/" in text
    assert "apt-get upgrade -y --no-install-recommends" in text
    assert "--without-hashes" not in text
    assert "USER 10001:10001" in text
    assert 'CMD ["uvicorn"' in text and '"--workers", "1"' in text
    assert "scrapeyard-entrypoint" in text
    assert "CAMOUFOX_BROWSER_SHA256=" in text
    assert "sha256sum --check --strict" in text
    assert "UBO_VERSION=1.72.2" in text
    assert "UBO_SHA256=40c315b0" in text
    assert "ublock_origin-${UBO_VERSION}.xpi" in text
    assert "REBROWSER_SANDBOX=" not in text
    assert "CHROME_DEVEL_SANDBOX" not in text
    assert "install -o root -g root -m 4755" not in text
    assert "chromium-${REBROWSER_CHROMIUM_REVISION}/chrome-linux" not in text
    assert "setpriv" not in text
    assert "chown -R scrapeyard:scrapeyard" not in text


def test_egress_policy_blocks_reserved_destinations_after_explicit_allows() -> None:
    path = Path("security/install-docker-egress-policy.sh")
    subprocess.run(["bash", "-n", str(path)], check=True)
    text = path.read_text(encoding="utf-8")
    assert "DOCKER-USER" in text
    assert '-i "$NETWORK_INTERFACE"' in text
    assert 'LEGACY_CHAIN="SY-EGRESS-${POLICY_ID}"' in text
    assert 'CHAIN_A="${LEGACY_CHAIN}-A"' in text
    assert 'CHAIN_B="${LEGACY_CHAIN}-B"' in text
    assert "--dport 6379 -j ACCEPT" in text
    # The forwarded policy for each address family, and the host chain.
    assert text.count("--ctstate ESTABLISHED,RELATED -j ACCEPT") == 3
    assert 'HOST_CHAIN_A="SY-HOST-${POLICY_ID}-A"' in text
    assert '"$tool" -I INPUT 1 -i "$NETWORK_INTERFACE" -j "$new"' in text
    assert 'PROBE_LIVENESS_PORT="${SCRAPEYARD_EGRESS_POLICY_PROBE_LIVENESS_PORT:-8081}"' in text
    assert '-p tcp --dport "$PROBE_LIVENESS_PORT" -j ACCEPT' in text
    rendered = subprocess.run(
        ["python3", "security/render-egress-policy.py"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    for cidr in (
        "10.0.0.0/8",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.2.0/24",
        "192.168.0.0/16",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "fc00::/7",
        "fe80::/10",
    ):
        assert cidr in rendered


def test_secure_compose_deployment_installs_policy_before_starting_app() -> None:
    path = Path("security/deploy-secure-compose.sh")
    subprocess.run(["bash", "-n", str(path)], check=True)
    text = path.read_text(encoding="utf-8")
    dependency_start = text.index('docker compose up -d --wait "${start_options[@]}" egress-probe redis')
    image_build = text.index("docker compose build scrapeyard")
    policy_install = text.index("install-docker-egress-policy.sh install")
    application_stop = text.index("stop_application || fail")
    application_start = text.index("docker compose up -d --wait", policy_install)

    assert image_build < application_stop < dependency_start < policy_install < application_start
    assert "SCRAPEYARD_PROXY_URL" in text
    assert "SCRAPEYARD_EGRESS_INTERFACE" in text
    assert "install-chromium-apparmor-profile.sh install" in text


def _run_secret_names_step(
    tmp_path: Path, *, check: bool = True, **settings: str
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run the wrapper's secret-name step on its own, as root's shell would."""
    text = Path("security/deploy-secure-compose.sh").read_text(encoding="utf-8")
    start = text.index("allowed_names=$(")
    step = text[start : text.index("\nfi\n", text.index("if ((${#secret_names[@]}", start)) + 4]
    lock_dir = tmp_path / "lock"
    lock_dir.mkdir(mode=0o700, exist_ok=True)
    done = subprocess.run(
        [
            "bash",
            "-euo",
            "pipefail",
            "-c",
            'fail() { echo "$*" >&2; exit 2; }\n' + step + 'printf %s "${COMPOSE_FILE:-}"',
        ],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "LOCK_DIR": str(lock_dir), **settings},
        capture_output=True,
        text=True,
        check=check,
    )
    return done, lock_dir / "secret-names.compose.yml"


_ALLOWLIST = (
    '{"eyebox":["SCRAPEYARD_SECRET_PROXY_URL","SCRAPEYARD_SECRET_UNDEFINED"],'
    '"*":["SCRAPEYARD_SECRET_WEBHOOK_AUTH"]}'
)


@pytest.mark.parametrize(
    "compose_file", [None, "/srv/releases/r1/source/docker-compose.yml:/srv/releases/r1/images.json"]
)
def test_secure_compose_deployment_hands_allowed_secret_names_to_compose_without_values(
    tmp_path: Path, compose_file: str | None
) -> None:
    settings = {
        "SCRAPEYARD_SECRET_PROXY_URL": "http://user:value-one@203.0.113.9:8080",
        "SCRAPEYARD_SECRET_WEBHOOK_AUTH": "Bearer value-two",
        # Defined, and no project may name it.
        "SCRAPEYARD_SECRET_UNUSED": "value-three",
        # Names, passed by the Compose file itself, and no reference target.
        "SCRAPEYARD_SECRET_REFERENCE_ALLOWLIST": _ALLOWLIST,
        "SCRAPEYARD_SECRETS_FILE": "value-four",
        "SCRAPEYARD_API_CREDENTIALS": "value-five",
        **({"COMPOSE_FILE": compose_file} if compose_file else {}),
    }

    done, names = _run_secret_names_step(tmp_path, **settings)

    assert done.stdout == f"{compose_file or 'docker-compose.yml'}:{names}"
    written = names.read_text(encoding="utf-8")
    assert "value" not in written
    environment = yaml.safe_load(written)["services"]["scrapeyard"]["environment"]
    assert sorted(environment) == ["SCRAPEYARD_SECRET_PROXY_URL", "SCRAPEYARD_SECRET_WEBHOOK_AUTH"]
    assert names.stat().st_mode & 0o777 == 0o600


def test_secure_compose_deployment_keeps_compose_s_own_override_file(tmp_path: Path) -> None:
    (tmp_path / "docker-compose.override.yml").write_text("services: {}\n")

    done, names = _run_secret_names_step(
        tmp_path,
        SCRAPEYARD_SECRET_PROXY_URL="http://user:value-one@203.0.113.9:8080",
        SCRAPEYARD_SECRET_REFERENCE_ALLOWLIST=_ALLOWLIST,
    )

    assert done.stdout == f"docker-compose.yml:docker-compose.override.yml:{names}"


@pytest.mark.parametrize(
    "settings",
    [
        {"SCRAPEYARD_SECRET_REFERENCE_ALLOWLIST": "{}", "COMPOSE_FILE": "a.yml:b.json"},
        # Defined secrets that no project may name.
        {"SCRAPEYARD_SECRET_PROXY_URL": "http://u:p@203.0.113.9:8080", "COMPOSE_FILE": "a.yml:b.json"},
    ],
)
def test_secure_compose_deployment_without_allowed_secrets_leaves_compose_files_alone(
    tmp_path: Path, settings: dict[str, str]
) -> None:
    done, names = _run_secret_names_step(tmp_path, **settings)

    assert done.stdout == "a.yml:b.json"
    assert not names.exists()


def test_secure_compose_deployment_refuses_compose_files_it_would_drop(tmp_path: Path) -> None:
    (tmp_path / "compose.override.yaml").write_text("services: {}\n")

    done, names = _run_secret_names_step(
        tmp_path,
        check=False,
        SCRAPEYARD_SECRET_PROXY_URL="http://user:value-one@203.0.113.9:8080",
        SCRAPEYARD_SECRET_REFERENCE_ALLOWLIST=_ALLOWLIST,
    )

    assert done.returncode == 2
    assert "compose.override.yaml is here" in done.stderr


def test_secure_compose_deployment_reads_a_blank_allowlist_as_empty(tmp_path: Path) -> None:
    done, names = _run_secret_names_step(
        tmp_path,
        SCRAPEYARD_SECRET_PROXY_URL="http://user:value-one@203.0.113.9:8080",
        SCRAPEYARD_SECRET_REFERENCE_ALLOWLIST="   ",
        COMPOSE_FILE="a.yml",
    )

    assert done.stdout == "a.yml"
    assert not names.exists()


def test_secure_compose_deployment_refuses_an_allowlist_it_cannot_read(tmp_path: Path) -> None:
    done, names = _run_secret_names_step(
        tmp_path,
        check=False,
        SCRAPEYARD_SECRET_PROXY_URL="http://u:p@203.0.113.9:8080",
        SCRAPEYARD_SECRET_REFERENCE_ALLOWLIST='["SCRAPEYARD_SECRET_PROXY_URL"]',
    )

    assert done.returncode == 2
    assert "allowlist could not be read" in done.stderr
    assert not names.exists()


def test_smoke_installs_connected_ip_policy_before_starting_app() -> None:
    text = Path("scripts/run_container_browser_smoke.sh").read_text(encoding="utf-8")
    dependency_start = text.index("egress-probe redis fixture")
    policy_install = text.index("run_egress_policy install", dependency_start)
    application_start = text.index("up -d --no-build scrapeyard peer", policy_install)

    assert dependency_start < policy_install < application_start


def test_security_scan_uses_digest_pinned_tools_and_enforces_findings() -> None:
    path = Path("scripts/run_container_security_scan.sh")
    subprocess.run(["bash", "-n", str(path)], check=True)
    text = path.read_text(encoding="utf-8")
    assert "anchore/syft:v1.27.1@sha256:" in text
    assert "aquasec/trivy:0.65.0@sha256:" in text
    assert "cyclonedx-json=/out/sbom.cdx.json" in text
    assert text.count("--exit-code 1") == 2
    assert "--scanners vuln" in text
    assert '"$TRIVY_IMAGE" config' in text
