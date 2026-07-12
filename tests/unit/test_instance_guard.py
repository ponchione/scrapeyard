from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scrapeyard.runtime.instance_guard import (
    SingleInstanceError,
    SingleInstanceLock,
    instance_identity,
    instance_lock_path,
    redis_deployment_identity,
    validate_single_process_configuration,
)


def _lock(db_dir: Path) -> SingleInstanceLock:
    identity = instance_identity(
        db_dir=str(db_dir),
        queue_name="shared-queue",
        redis_dsn="redis://user:secret@redis.example:6380/7",
    )
    return SingleInstanceLock(instance_lock_path(str(db_dir)), identity)


def test_lock_rejects_contender_and_can_be_reacquired_after_shutdown(tmp_path: Path) -> None:
    first = _lock(tmp_path / "db")
    second = _lock(tmp_path / "db")
    first.acquire()
    try:
        metadata = json.loads(first.path.read_text(encoding="utf-8"))
        assert metadata["pid"] == os.getpid()
        assert metadata["identity"]["queue_name"] == "shared-queue"
        assert metadata["identity"]["redis"] == "redis://redis.example:6380/7"
        assert "secret" not in first.path.read_text(encoding="utf-8")
        assert stat.S_IMODE(first.path.stat().st_mode) == 0o600

        with pytest.raises(SingleInstanceError, match="Another Scrapeyard") as error:
            second.acquire()
        assert "workers/replicas at 1" in str(error.value)
        assert f"pid={os.getpid()}" in str(error.value)
    finally:
        first.release()

    second.acquire()
    assert second.acquired is True
    second.release()
    assert second.acquired is False


def test_stale_lock_file_is_overwritten_when_no_process_holds_it(tmp_path: Path) -> None:
    lock = _lock(tmp_path / "db")
    lock.path.parent.mkdir(parents=True)
    lock.path.write_text('{"pid": 999999, "hostname": "stale"}\n', encoding="utf-8")

    lock.acquire()
    try:
        metadata = json.loads(lock.path.read_text(encoding="utf-8"))
        assert metadata["pid"] == os.getpid()
        assert metadata["hostname"] != "stale"
    finally:
        lock.release()


def test_kernel_releases_lock_after_process_crash(tmp_path: Path) -> None:
    db_dir = tmp_path / "db"
    code = (
        "import os; from pathlib import Path; "
        "from scrapeyard.runtime.instance_guard import ("
        "SingleInstanceLock, instance_identity, instance_lock_path); "
        f"db={str(db_dir)!r}; "
        "lock=SingleInstanceLock(instance_lock_path(db), "
        "instance_identity(db_dir=db, queue_name='shared-queue', "
        "redis_dsn='redis://redis:6379/0')); "
        "lock.acquire(); os._exit(0)"
    )
    subprocess.run([sys.executable, "-c", code], check=True)

    recovered = _lock(db_dir)
    recovered.acquire()
    recovered.release()


@pytest.mark.parametrize(
    ("environment", "arguments", "source"),
    [
        ({"WEB_CONCURRENCY": "2"}, [], "WEB_CONCURRENCY"),
        ({"UVICORN_WORKERS": "4"}, [], "UVICORN_WORKERS"),
        ({"GUNICORN_WORKERS": "3"}, [], "GUNICORN_WORKERS"),
        ({"UVICORN_CMD_ARGS": "--workers=2"}, [], "UVICORN_CMD_ARGS"),
        ({"GUNICORN_CMD_ARGS": "-w 2"}, [], "GUNICORN_CMD_ARGS"),
        ({}, ["uvicorn", "scrapeyard.main:app", "--workers", "2"], "server command"),
        ({}, ["gunicorn", "scrapeyard.main:app", "-w2"], "server command"),
    ],
)
def test_multi_process_configuration_is_rejected(
    environment: dict[str, str],
    arguments: list[str],
    source: str,
) -> None:
    with pytest.raises(SingleInstanceError, match=source):
        validate_single_process_configuration(
            environment=environment,
            arguments=arguments,
        )


def test_one_worker_configuration_and_scrape_concurrency_are_allowed() -> None:
    validate_single_process_configuration(
        environment={
            "WEB_CONCURRENCY": "1",
            "UVICORN_CMD_ARGS": "--workers 1",
            "SCRAPEYARD_WORKERS_MAX_CONCURRENT": "12",
        },
        arguments=["uvicorn", "scrapeyard.main:app", "--workers=1"],
    )


def test_redis_identity_never_includes_credentials() -> None:
    assert (
        redis_deployment_identity("redis://user:secret@redis.example:6380/7?ssl=true")
        == "redis://redis.example:6380/7"
    )


def test_guard_cli_returns_configuration_error_for_multiple_workers() -> None:
    environment = {
        name: value
        for name, value in os.environ.items()
        if name
        not in {
            "WEB_CONCURRENCY",
            "UVICORN_WORKERS",
            "GUNICORN_WORKERS",
            "UVICORN_CMD_ARGS",
            "GUNICORN_CMD_ARGS",
        }
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scrapeyard.runtime.instance_guard",
            "--",
            "uvicorn",
            "scrapeyard.main:app",
            "--workers",
            "2",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 78
    assert "fatal single-process configuration" in result.stderr
    assert "SCRAPEYARD_WORKERS_MAX_CONCURRENT" in result.stderr


def test_deployment_surfaces_pin_one_process_and_one_replica() -> None:
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    service = compose["services"]["scrapeyard"]
    assert service["deploy"]["replicas"] == 1
    assert service["command"][-2:] == ["--workers", "1"]

    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert '"--workers", "1"' in dockerfile
    entrypoint = Path("scripts/container-entrypoint.sh").read_text(encoding="utf-8")
    assert 'python -m scrapeyard.runtime.instance_guard -- "$@"' in entrypoint

    readme = Path("README.md").read_text(encoding="utf-8")
    deployment = Path("docs/DEPLOYMENT.md").read_text(encoding="utf-8")
    scaling = Path("docs/SCALING.md").read_text(encoding="utf-8")
    assert "docs/SCALING.md" in readme
    assert ".scrapeyard-instance.lock" in deployment
    for state_scope in ("Process-local", "Redis-shared", "SQLite-shared", "Filesystem-shared"):
        assert state_scope in scaling
