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
        '{"compose-test":{"secret":"0123456789abcdef","scopes":["submit"]}}'
    ),
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
    assert all(
        expression.startswith(f"${{{name}:")
        for name, expression in environment.items()
    )


def test_production_compose_preserves_defaults_and_honors_overrides(tmp_path: Path) -> None:
    defaults = _render_production_compose(tmp_path)
    assert defaults["SCRAPEYARD_WORKERS_MAX_CONCURRENT"] == "4"
    assert defaults["SCRAPEYARD_WORKERS_MEMORY_LIMIT_MB"] == "3072"
    assert defaults["SCRAPEYARD_HEALTH_PROBE_TIMEOUT_SECONDS"] == "2"
    assert defaults["SCRAPEYARD_PROXY_URL"] == "http://8.8.8.8:8080"
    assert defaults["SCRAPEYARD_UNTRUSTED_SUBMISSIONS"] == "true"
    assert defaults["SCRAPEYARD_EGRESS_POLICY_PROBE_HOST"] == "172.30.0.248"

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
