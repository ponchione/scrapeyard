from __future__ import annotations

import ipaddress
import json
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yaml

from tests.smoke.fixture_site import PRIVATE_ORIGIN, PUBLIC_ORIGIN, create_server
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
    assert "src/scrapeyard/main.py" in paths
    assert "src/scrapeyard/runtime/**" in paths
    assert "src/scrapeyard/scheduler/**" in paths
    assert "src/scrapeyard/webhook/**" in paths
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
    assert artifact["with"]["path"] == "artifacts/container-browser-smoke/"


def test_dockerfile_has_immutable_inputs_and_non_root_runtime_contract() -> None:
    text = Path("Dockerfile").read_text(encoding="utf-8")
    assert "python:3.12.11-slim-bookworm@sha256:" in text
    assert "ARG DEBIAN_SNAPSHOT=20260715T000000Z" in text
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
    assert text.count("--ctstate ESTABLISHED,RELATED -j ACCEPT") == 2
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
    dependency_start = text.index("docker compose up -d --wait egress-probe redis")
    image_build = text.index("docker compose build scrapeyard")
    policy_install = text.index("install-docker-egress-policy.sh install")
    application_stop = text.index("docker compose stop scrapeyard")
    application_start = text.index("docker compose up -d --wait", policy_install)

    assert image_build < application_stop < dependency_start < policy_install < application_start
    assert "SCRAPEYARD_PROXY_URL" in text
    assert "SCRAPEYARD_EGRESS_INTERFACE" in text
    assert "install-chromium-apparmor-profile.sh install" in text


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
