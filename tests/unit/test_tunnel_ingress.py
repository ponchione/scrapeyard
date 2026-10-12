from __future__ import annotations

import re
from pathlib import Path

import yaml
from fastapi.routing import APIRoute

from scrapeyard.main import app

UNIT = Path("security/systemd/cloudflared-scrapeyard.service")
ROUTES = Path("security/cloudflared/ingress.example.yml")

# The paths Eyebox calls (TECH_DEBT.md, "Eyebox contract"), plus liveness.
EYEBOX_PATHS = {
    "/health",
    "/scrape",
    "/scrape/acceptance",
    "/jobs/{job_id}",
    "/jobs/{job_id}/runs/{run_id}",
    "/jobs/{job_id}/cancel",
    "/results/{job_id}",
}


def _rules() -> list[dict]:
    return yaml.safe_load(ROUTES.read_text(encoding="utf-8"))["ingress"]


def _unit() -> dict[str, list[str]]:
    settings: dict[str, list[str]] = {}
    for line in UNIT.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            name, value = line.split("=", 1)
            settings.setdefault(name, []).append(value)
    return settings


def test_tunnel_forwards_the_paths_eyebox_calls_and_no_other_route_of_the_application() -> None:
    forwarded, fallback = _rules()
    path = re.compile(forwarded["path"])
    assert forwarded["path"].startswith("^/") and forwarded["path"].endswith("$")
    assert fallback == {"service": "http_status:404"}

    admitted = set()
    for route in app.routes:
        # Every route, documentation pages included: a new one is closed to
        # the tunnel until someone adds it to the list on purpose.
        concrete = re.sub(r"\{[^}]+\}", "7c9e6679-7425-40de-944b-e07fc1f90ae7", route.path)
        if path.search(concrete):
            admitted.add(route.path)

    assert admitted == EYEBOX_PATHS
    assert {route.path for route in app.routes if isinstance(route, APIRoute)} >= EYEBOX_PATHS


def test_tunnel_refuses_odd_spellings_of_an_allowed_path() -> None:
    path = re.compile(_rules()[0]["path"])

    for spelling in (
        "/health/",
        "//health",
        "/HEALTH",
        "/health/ready",
        "/jobs",
        "/jobs/",
        "/jobs/a/pause",
        "/jobs/a/runs/b/c",
        "/results/a/b",
        "/scrape/acceptance/",
        "/health\n/metrics",
    ):
        assert not path.search(spelling), spelling


def test_connector_checks_the_access_token_and_reaches_only_the_application() -> None:
    forwarded = _rules()[0]
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    unit = _unit()

    # A request that reached the tunnel without passing Access is refused.
    assert forwarded["originRequest"]["access"]["required"] is True
    assert forwarded["originRequest"]["access"]["audTag"]
    # The application's fixed address on the deployment bridge, and port.
    assert forwarded["service"] == "http://172.30.0.250:8420"
    assert "ipv4_address: 172.30.0.250" in compose
    # systemd grants what the allow list names before it reads the deny list:
    # the application and the local resolver, and nothing wider.
    assert unit["IPAddressAllow"] == ["172.30.0.250/32 127.0.0.53/32 127.0.0.54/32"]
    (denied,) = unit["IPAddressDeny"]
    for network in ("127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                    "169.254.0.0/16", "100.64.0.0/10", "fe80::/10", "fc00::/7"):
        assert network in denied.split()


def test_connector_runs_without_privilege_and_keeps_its_token_out_of_its_command_line() -> None:
    unit = _unit()

    assert unit["User"] == ["cloudflared"]
    assert unit["NoNewPrivileges"] == ["yes"]
    assert unit["CapabilityBoundingSet"] == [""]
    assert unit["ProtectSystem"] == ["strict"]
    assert unit["LoadCredential"] == ["tunnel-token:/etc/cloudflared/tunnel-token"]
    (command,) = unit["ExecStart"]
    assert "--token-file %d/tunnel-token" in command
    assert "--token " not in command and "TUNNEL_TOKEN" not in UNIT.read_text(encoding="utf-8")
    # Docker's socket and the deployment's secrets are not this process's to see.
    (hidden,) = unit["InaccessiblePaths"]
    assert {"-/run/docker.sock", "-/etc/scrapeyard", "-/srv/scrapeyard-releases"} <= set(
        hidden.split()
    )
