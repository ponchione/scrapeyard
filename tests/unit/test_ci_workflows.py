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
        "poetry run ruff check src tests scripts security",
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

    live_redis = workflow["jobs"]["live-redis"]
    assert live_redis["services"]["redis"]["image"] == (
        "redis:7.4.11-alpine@sha256:"
        "858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499"
    )
    assert live_redis["env"]["SCRAPEYARD_API_CREDENTIALS"]
    assert live_redis["env"]["SCRAPEYARD_REDIS_DSN"].endswith("/15")
    assert live_redis["env"]["SCRAPEYARD_LIVE_REDIS_REQUIRED"] == "1"
    live_commands = _run_commands(live_redis)
    assert "python scripts/check_packaging_toolchain.py" in live_commands
    assert "pytest -W error --no-cov -m live_redis tests/live_redis" in live_commands
    # tests/live_redis asserts appendonly, which a service container can't be
    # started with, so it is set before the tests run.
    step_names = [step.get("name", "") for step in live_redis["steps"]]
    persistence = step_names.index("Enable Redis append-only persistence")
    assert persistence < step_names.index("Exercise the authenticated real Redis/arq path")
    assert (
        'docker exec "${{ job.services.redis.id }}" redis-cli CONFIG SET appendonly yes'
        in live_redis["steps"][persistence]["run"]
    )


def test_ci_lets_the_real_browser_tests_start_a_sandboxed_chromium() -> None:
    quality = _workflow("ci.yml")["jobs"]["quality"]
    install = next(
        step for step in quality["steps"] if step.get("name", "").startswith("Install Chromium")
    )
    assert install["if"] == "matrix.python-version == '3.12'"
    assert "playwright install --with-deps chromium" in install["run"]
    # Scrapeyard never starts Chromium with --no-sandbox, so the runner has to
    # allow the user namespaces the sandbox is built from.
    assert "sysctl -w kernel.apparmor_restrict_unprivileged_userns=0" in install["run"]


def test_ci_type_checks_once_against_the_configured_target() -> None:
    quality = _workflow("ci.yml")["jobs"]["quality"]
    type_check = next(step for step in quality["steps"] if step.get("name") == "Type check")
    # mypy's python_version, not the interpreter, decides what it checks.
    assert 'python_version = "3.10"' in Path("pyproject.toml").read_text(encoding="utf-8")
    assert type_check["if"] == "matrix.python-version == '3.10'"
    assert type_check["run"] == "poetry run mypy src"


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
    # Packages the runner or virtualenv preinstall are audited too, so the
    # unused setuptools is removed and Poetry's seeded pip gets the pinned one.
    assert commands.index("pip uninstall -y setuptools") < commands.index(
        "python -m pip_audit --local"
    )
    assert commands.index("poetry run python -m pip install --upgrade pip==") < commands.index(
        "audit_dependencies.py all"
    )


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
    assert (
        'run_container_security_scan.sh scrapeyard:security-scan "${dependencies[@]}"'
        in commands
    )
    assert "config --images redis egress-probe" in commands
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
