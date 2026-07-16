"""Contracts for the production Compose settings surface."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

from scrapeyard.common.settings import ServiceSettings


_QUALIFICATION_ONLY_SETTINGS = frozenset(
    {
        "SCRAPEYARD_QUALIFICATION_MODE",
        "SCRAPEYARD_QUALIFICATION_CRASH_POINT",
        "SCRAPEYARD_QUALIFICATION_MARKER_DIR",
    }
)
_REQUIRED_ENV = {
    "SCRAPEYARD_API_CREDENTIALS": (
        '{"compose-test":{"secret":"0123456789abcdef","scopes":["submit","read"]},'
        '"compose-health":{"secret":"health-key-0123456789","scopes":["health-detail"]}}'
    ),
    "SCRAPEYARD_HEALTH_PROBE_API_KEY": "health-key-0123456789",
    "SCRAPEYARD_ENCRYPTION_KEYS": (
        '{"v1":"MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="}'
    ),
    "SCRAPEYARD_ENCRYPTION_ACTIVE_KEY_ID": "v1",
    "SCRAPEYARD_PROXY_URL": "http://8.8.8.8:8080",
}


def _production_environment_source() -> dict[str, str]:
    compose = yaml.load(
        Path("docker-compose.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    return compose["services"]["scrapeyard"]["environment"]


def _render_production_compose(tmp_path: Path, **overrides: str) -> dict[str, str]:
    empty_env = tmp_path / "empty-compose.env"
    empty_env.write_text("", encoding="utf-8")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("SCRAPEYARD_")
    }
    environment.update(_REQUIRED_ENV)
    environment.update(overrides)
    rendered = subprocess.run(
        ["docker", "compose", "--env-file", str(empty_env), "config"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    compose = yaml.safe_load(rendered.stdout)
    return compose["services"]["scrapeyard"]["environment"]


def test_production_compose_enumerates_every_runtime_setting() -> None:
    environment = _production_environment_source()
    model_settings = {
        f"SCRAPEYARD_{name.upper()}" for name in ServiceSettings.model_fields
    }

    assert set(environment) == model_settings - _QUALIFICATION_ONLY_SETTINGS
    assert _QUALIFICATION_ONLY_SETTINGS.isdisjoint(environment)
    assert environment["SCRAPEYARD_LOCAL_DEVELOPMENT_UNAUTHENTICATED"] == "false"
    assert all(
        expression.startswith(f"${{{name}:")
        for name, expression in environment.items()
        if name != "SCRAPEYARD_LOCAL_DEVELOPMENT_UNAUTHENTICATED"
    )


def test_production_compose_preserves_defaults_and_honors_overrides(tmp_path: Path) -> None:
    defaults = _render_production_compose(tmp_path)
    assert defaults["SCRAPEYARD_WORKERS_MAX_CONCURRENT"] == "4"
    assert defaults["SCRAPEYARD_WORKERS_MEMORY_LIMIT_MB"] == "3072"
    assert defaults["SCRAPEYARD_HEALTH_PROBE_TIMEOUT_SECONDS"] == "2"
    assert defaults["SCRAPEYARD_PROXY_URL"] == "http://8.8.8.8:8080"
    assert defaults["SCRAPEYARD_UNTRUSTED_SUBMISSIONS"] == "true"
    assert defaults["SCRAPEYARD_EGRESS_POLICY_PROBE_HOST"] == "172.30.0.248"
    assert defaults["SCRAPEYARD_EGRESS_POLICY_PROBE_PORT"] == "8080"
    assert defaults["SCRAPEYARD_EGRESS_POLICY_PROBE_LIVENESS_PORT"] == "8081"
    assert defaults["SCRAPEYARD_LOCAL_DEVELOPMENT_UNAUTHENTICATED"] == "false"
    assert defaults["SCRAPEYARD_HEALTH_PROBE_API_KEY"] == "health-key-0123456789"

    overridden = _render_production_compose(
        tmp_path,
        SCRAPEYARD_WORKERS_MAX_CONCURRENT="9",
        SCRAPEYARD_HEALTH_PROBE_TIMEOUT_SECONDS="7",
        SCRAPEYARD_PROXY_URL="https://proxy.example:8443",
    )
    assert overridden["SCRAPEYARD_WORKERS_MAX_CONCURRENT"] == "9"
    assert overridden["SCRAPEYARD_HEALTH_PROBE_TIMEOUT_SECONDS"] == "7"
    assert overridden["SCRAPEYARD_PROXY_URL"] == "https://proxy.example:8443"


def test_trusted_compose_overlays_explicitly_disable_production_attestation() -> None:
    for path in ("docker-compose.local.yml", "docker-compose.qualification.yml"):
        compose = yaml.load(Path(path).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        environment = compose["services"]["scrapeyard"]["environment"]
        assert environment["SCRAPEYARD_UNTRUSTED_SUBMISSIONS"] == "false"
        assert environment["SCRAPEYARD_EGRESS_POLICY_PROBE_HOST"] == ""
        assert environment["SCRAPEYARD_EGRESS_POLICY_PROBE_PORT"] == "0"
        assert environment["SCRAPEYARD_EGRESS_POLICY_PROBE_LIVENESS_PORT"] == "0"


def test_local_overlay_explicitly_opts_into_unauthenticated_development() -> None:
    local = yaml.load(
        Path("docker-compose.local.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )["services"]["scrapeyard"]["environment"]

    assert local["SCRAPEYARD_LOCAL_DEVELOPMENT_UNAUTHENTICATED"] == "true"
    assert local["SCRAPEYARD_API_CREDENTIALS"] == ""
    assert local["SCRAPEYARD_HEALTH_PROBE_API_KEY"] == ""


def test_production_compose_bounds_camoufox_writable_runtime_state() -> None:
    compose = yaml.load(
        Path("docker-compose.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    tmpfs = compose["services"]["scrapeyard"]["tmpfs"]

    assert any(
        value.startswith("/home/scrapeyard/camoufox:")
        and "noexec" in value
        and "uid=10001" in value
        and "gid=10001" in value
        for value in tmpfs
    )
    assert any(
        value.startswith("/opt/scrapeyard-cache/fontconfig:")
        and "noexec" in value
        and "uid=10001" in value
        and "gid=10001" in value
        for value in tmpfs
    )


def test_live_redis_runner_uses_an_isolated_compose_network() -> None:
    script = Path("scripts/run_live_redis_tests.sh").read_text(encoding="utf-8")

    for setting in (
        "SCRAPEYARD_TEST_BACKEND_SUBNET",
        "SCRAPEYARD_TEST_BACKEND_IP_RANGE",
        "SCRAPEYARD_TEST_EGRESS_POLICY_PROBE_HOST",
        "SCRAPEYARD_TEST_REDIS_DESTINATION",
        "SCRAPEYARD_TEST_EGRESS_SOURCE",
    ):
        assert setting in script
    assert "172.29.13.0/24" in script
    assert "-u SCRAPEYARD_EGRESS_POLICY_PROBE_HOST" in script


def test_qualification_overlay_allows_the_disk_failure_injection() -> None:
    qualification = yaml.load(
        Path("docker-compose.qualification.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )

    assert qualification["services"]["scrapeyard"]["environment"][
        "SCRAPEYARD_HEALTH_DISK_FREE_MIN_MB"
    ] == "${SCRAPEYARD_HEALTH_DISK_FREE_MIN_MB:-10}"
