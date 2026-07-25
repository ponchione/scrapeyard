from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml


WORKFLOWS = Path(".github/workflows")


def _workflow(name: str) -> dict[str, Any]:
    # BaseLoader keeps GitHub's `on` key as a string instead of applying
    # YAML 1.1's obsolete boolean conversion.
    loaded = yaml.load((WORKFLOWS / name).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(loaded, dict)
    return loaded


def _run_commands(job: dict[str, Any]) -> str:
    return "\n".join(step.get("run", "") for step in job["steps"])


def test_ci_required_gate_contracts() -> None:
    workflow = _workflow("ci.yml")
    assert set(workflow["on"]) == {"pull_request", "push", "workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] == "true"

    quality = workflow["jobs"]["quality"]
    assert quality["strategy"]["matrix"]["python-version"] == ["3.10", "3.12"]
    quality_commands = _run_commands(quality)
    for command in (
        "poetry check --lock",
        "poetry sync --no-interaction",
        "python scripts/check_packaging_toolchain.py",
        "poetry run ruff check src tests scripts",
        "scripts/audit_browser_security.py --dockerfile Dockerfile",
        "poetry run mypy src",
        "poetry run pytest",
    ):
        assert command in quality_commands

    build_commands = _run_commands(workflow["jobs"]["build"])
    assert "python scripts/check_packaging_toolchain.py" in build_commands
    assert "poetry build" in build_commands
    assert "python scripts/inspect_distribution.py dist" in build_commands
    assert "python scripts/smoke_install_distribution.py dist" in build_commands
    assert build_commands.count("audit_dependencies.py export-production") == 2
    assert 'cmp "${export_dir}/first.txt" "${export_dir}/second.txt"' in build_commands

    browser_security = workflow["jobs"]["browser-security"]
    browser_commands = _run_commands(browser_security)
    assert "playwright install --with-deps chromium" in browser_commands
    assert "pytest -W error --no-cov -m live_browser tests/live_browser" in browser_commands
    assert browser_security["steps"][-2]["env"]["SCRAPEYARD_RUN_LIVE_BROWSER"] == "1"

    live_redis = workflow["jobs"]["live-redis"]
    assert live_redis["services"]["redis"]["image"] == (
        "redis:7.4.9-alpine3.21@sha256:"
        "6ab0b6e7381779332f97b8ca76193e45b0756f38d4c0dcda72dbb3c32061ab99"
    )
    assert live_redis["env"]["SCRAPEYARD_API_CREDENTIALS"]
    assert live_redis["env"]["SCRAPEYARD_HEALTH_PROBE_API_KEY"]
    assert live_redis["env"]["SCRAPEYARD_REDIS_DSN"].endswith("/15")
    live_commands = _run_commands(live_redis)
    assert "python scripts/check_packaging_toolchain.py" in live_commands
    assert "pytest -W error --no-cov -m live_redis tests/live_redis" in live_commands


def test_ci_caches_only_dependency_downloads() -> None:
    for filename in ("ci.yml", "dependency-audit.yml"):
        workflow = _workflow(filename)
        cache_steps = [
            step
            for job in workflow["jobs"].values()
            for step in job["steps"]
            if step.get("uses", "").startswith("actions/cache@")
        ]
        assert cache_steps
        for step in cache_steps:
            assert set(step["with"]["path"].splitlines()) == {
                "~/.cache/pip",
                "~/.cache/pypoetry/artifacts",
            }


def test_dependency_audit_remains_scheduled_and_required() -> None:
    workflow = _workflow("dependency-audit.yml")
    assert set(workflow["on"]) == {
        "pull_request",
        "push",
        "schedule",
        "workflow_dispatch",
    }
    assert workflow["on"]["schedule"] == [{"cron": "23 9 * * 1"}]
    assert workflow["permissions"] == {"contents": "read"}
    assert "github.event_name" in workflow["concurrency"]["group"]
    assert workflow["jobs"]["dependency-audit"]["strategy"]["matrix"][
        "python-version"
    ] == ["3.10", "3.12"]

    commands = _run_commands(workflow["jobs"]["dependency-audit"])
    assert "python scripts/check_packaging_toolchain.py" in commands
    assert "python -m pip_audit --local --progress-spinner=off" in commands
    assert "poetry run python scripts/audit_dependencies.py all" in commands


def test_container_security_workflow_builds_sbom_and_enforces_scans() -> None:
    workflow = _workflow("container-security.yml")
    assert set(workflow["on"]) == {
        "pull_request",
        "push",
        "schedule",
        "workflow_dispatch",
    }
    assert workflow["permissions"] == {"contents": "read"}
    commands = _run_commands(workflow["jobs"]["scan"])
    assert "docker build --pull --tag scrapeyard:security-scan ." in commands
    assert "run_container_security_scan.sh scrapeyard:security-scan" in commands
    artifact = next(
        step
        for step in workflow["jobs"]["scan"]["steps"]
        if step.get("uses", "").startswith("actions/upload-artifact@")
    )
    assert artifact["if"] == "always()"


def test_all_third_party_actions_are_pinned_to_full_commit_shas() -> None:
    for path in WORKFLOWS.glob("*.yml"):
        workflow = _workflow(path.name)
        for job in workflow["jobs"].values():
            for step in job["steps"]:
                uses = step.get("uses", "")
                if uses.startswith("actions/"):
                    assert re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", uses), (
                        path,
                        uses,
                    )


def test_immutable_release_workflow_builds_once_and_promotes_only_qualified_bytes() -> None:
    workflow = _workflow("release.yml")
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] == "false"
    qualify = workflow["jobs"]["qualify"]
    commands = _run_commands(qualify)
    assert commands.count("docker build --pull --no-cache") == 1
    assert "run_container_security_scan.sh \"${CANDIDATE_IMAGE_ID}\"" in commands
    assert commands.count('--image "${CANDIDATE_IMAGE_ID}"') == 4
    assert "--profile full" in commands
    assert "candidate-image.tar.zst.sha256" in commands
    artifact = next(
        step
        for step in qualify["steps"]
        if step.get("uses", "").startswith("actions/upload-artifact@")
    )
    assert artifact["with"]["retention-days"] == "3"

    promote = workflow["jobs"]["promote"]
    assert promote["if"] == "inputs.promote"
    assert promote["needs"] == "qualify"
    assert promote["environment"] == "production-release"
    assert promote["permissions"] == {"contents": "read", "packages": "write"}
    promote_commands = _run_commands(promote)
    assert "sha256sum --check candidate-image.tar.zst.sha256" in promote_commands
    assert "docker push" in promote_commands
