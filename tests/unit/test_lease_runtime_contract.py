"""Deployment-surface contract for explicit queued and running lease settings."""

from pathlib import Path


LEASE_ENV = {
    "SCRAPEYARD_WORKERS_QUEUED_CLAIM_TIMEOUT_SECONDS": "300",
    "SCRAPEYARD_WORKERS_RUNNING_HEARTBEAT_TIMEOUT_SECONDS": "600",
    "SCRAPEYARD_WORKERS_HEARTBEAT_INTERVAL_SECONDS": "30",
}


def test_compose_wires_explicit_lease_settings() -> None:
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")

    for name, default in LEASE_ENV.items():
        assert f'{name}: "{default}"' in compose
    assert "WORKERS_RUNNING_LEASE_SECONDS" not in compose


def test_readme_documents_explicit_lease_settings() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")

    for name, default in LEASE_ENV.items():
        assert name in readme
        assert f"`{default}`" in readme
    assert "WORKERS_RUNNING_LEASE_SECONDS" not in readme


def test_deployment_guide_documents_heartbeat_failure_policy() -> None:
    deployment = Path("docs/DEPLOYMENT.md").read_text(encoding="utf-8")

    for name in LEASE_ENV:
        assert name in deployment
    assert "One write failure does not abandon a run" in deployment
    assert "discards any unfinalized" in deployment
