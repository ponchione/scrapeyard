"""Single-process configuration validation and shared-state instance locking."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import shlex
import socket
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

INSTANCE_LOCK_NAME = ".scrapeyard-instance.lock"
_WORKER_ENV_VARS = ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS")
_SERVER_ARGS_ENV_VARS = ("UVICORN_CMD_ARGS", "GUNICORN_CMD_ARGS")


class SingleInstanceError(RuntimeError):
    """Raised when process configuration or lock ownership would be unsafe."""


@dataclass(frozen=True, slots=True)
class InstanceIdentity:
    """Non-secret description of the state and queue protected by one lock."""

    db_dir: str
    queue_name: str
    redis: str


@dataclass(frozen=True, slots=True)
class LockOwner:
    """Diagnostic owner metadata stored while the advisory lock is held."""

    pid: int
    hostname: str
    acquired_at: str
    identity: InstanceIdentity


def redis_deployment_identity(dsn: str) -> str:
    """Return a credential-free Redis endpoint/database identity."""

    parsed = urlsplit(dsn)
    hostname = parsed.hostname or "unknown-host"
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    port = f":{parsed.port}" if parsed.port is not None else ""
    database = parsed.path or "/0"
    return f"{parsed.scheme or 'redis'}://{hostname}{port}{database}"


def instance_identity(*, db_dir: str, queue_name: str, redis_dsn: str) -> InstanceIdentity:
    return InstanceIdentity(
        db_dir=str(Path(db_dir).resolve()),
        queue_name=queue_name,
        redis=redis_deployment_identity(redis_dsn),
    )


def instance_lock_path(db_dir: str) -> Path:
    """Locate the guard inside the SQLite state directory shared by replicas."""

    return Path(db_dir) / INSTANCE_LOCK_NAME


def _worker_count(value: str, source: str) -> int:
    try:
        count = int(value.strip())
    except ValueError as exc:
        raise SingleInstanceError(
            f"{source} must be the integer 1; Scrapeyard embeds one scheduler and worker pool"
        ) from exc
    if count != 1:
        raise SingleInstanceError(
            f"{source}={value!r} requests {count} application processes; "
            "Scrapeyard supports exactly one. Use SCRAPEYARD_WORKERS_MAX_CONCURRENT "
            "to change scrape concurrency, not Uvicorn/Gunicorn workers."
        )
    return count


def _validate_worker_arguments(arguments: Sequence[str], source: str) -> None:
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        value: str | None = None
        if argument in {"--workers", "-w"}:
            index += 1
            if index >= len(arguments):
                raise SingleInstanceError(f"{source} {argument} requires the value 1")
            value = arguments[index]
        elif argument.startswith("--workers="):
            value = argument.partition("=")[2]
        elif argument.startswith("-w") and argument != "-w":
            value = argument[2:]
        if value is not None:
            _worker_count(value, f"{source} {argument}")
        index += 1


def validate_single_process_configuration(
    *,
    environment: Mapping[str, str] | None = None,
    arguments: Sequence[str] | None = None,
) -> None:
    """Reject common Uvicorn/Gunicorn settings that create multiple processes."""

    env = os.environ if environment is None else environment
    for name in _WORKER_ENV_VARS:
        value = env.get(name)
        if value is not None and value.strip():
            _worker_count(value, name)

    for name in _SERVER_ARGS_ENV_VARS:
        value = env.get(name)
        if not value:
            continue
        try:
            configured_arguments = shlex.split(value)
        except ValueError as exc:
            raise SingleInstanceError(f"Unable to parse {name}: {exc}") from exc
        _validate_worker_arguments(configured_arguments, name)

    if arguments is not None:
        _validate_worker_arguments(arguments, "server command")


class SingleInstanceLock:
    """Kernel-released exclusive lock for one shared Scrapeyard state directory."""

    def __init__(self, path: Path, identity: InstanceIdentity) -> None:
        self.path = path
        self.identity = identity
        self._descriptor: int | None = None

    @property
    def acquired(self) -> bool:
        return self._descriptor is not None

    def _current_owner(self, descriptor: int) -> str:
        os.lseek(descriptor, 0, os.SEEK_SET)
        raw = os.read(descriptor, 4096).decode("utf-8", errors="replace").strip()
        if not raw:
            return "owner metadata unavailable"
        try:
            document = json.loads(raw)
        except json.JSONDecodeError:
            return "owner metadata unreadable"
        pid = document.get("pid", "unknown") if isinstance(document, dict) else "unknown"
        host = document.get("hostname", "unknown") if isinstance(document, dict) else "unknown"
        acquired = (
            document.get("acquired_at", "unknown") if isinstance(document, dict) else "unknown"
        )
        return f"pid={pid} host={host} acquired_at={acquired}"

    def acquire(self) -> None:
        """Acquire without waiting; an active peer is a fatal configuration error."""

        if self._descriptor is not None:
            raise SingleInstanceError(f"Single-instance lock is already acquired: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.set_inheritable(descriptor, False)
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise
                owner_detail = self._current_owner(descriptor)
                raise SingleInstanceError(
                    "Another Scrapeyard application process owns the shared-state lock "
                    f"{self.path} ({owner_detail}). Stop the other process and keep Uvicorn "
                    "workers/replicas at 1. For a truly isolated deployment, use distinct "
                    "SCRAPEYARD_DB_DIR, result/adaptive/log directories, Redis database, "
                    "and SCRAPEYARD_QUEUE_NAME values."
                ) from exc

            lock_owner = LockOwner(
                pid=os.getpid(),
                hostname=socket.gethostname(),
                acquired_at=datetime.now(timezone.utc).isoformat(),
                identity=self.identity,
            )
            payload = json.dumps(asdict(lock_owner), sort_keys=True).encode("utf-8") + b"\n"
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, payload)
            os.fsync(descriptor)
            self._descriptor = descriptor
        except Exception:
            os.close(descriptor)
            raise

    def release(self) -> None:
        """Clear diagnostics and release; the persistent inode avoids unlink races."""

        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        try:
            os.ftruncate(descriptor, 0)
            os.fsync(descriptor)
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def main() -> int:
    arguments = sys.argv[1:]
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    try:
        validate_single_process_configuration(arguments=arguments)
    except SingleInstanceError as exc:
        print(f"scrapeyard: fatal single-process configuration: {exc}", file=sys.stderr)
        return 78
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
