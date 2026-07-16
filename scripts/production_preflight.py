"""Fail-closed host, Compose, credential, and immutable-image production preflight."""

from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping


class PreflightError(RuntimeError):
    pass


def _required_env(environment: Mapping[str, Any], name: str) -> str:
    value = str(environment.get(name, "")).strip()
    if not value:
        raise PreflightError(f"{name} is required")
    return value


def validate_credentials(environment: Mapping[str, Any]) -> None:
    if str(environment.get("SCRAPEYARD_LOCAL_DEVELOPMENT_UNAUTHENTICATED", "")).lower() != "false":
        raise PreflightError("production must disable local unauthenticated mode")
    raw = _required_env(environment, "SCRAPEYARD_API_CREDENTIALS")
    probe_key = _required_env(environment, "SCRAPEYARD_HEALTH_PROBE_API_KEY")
    try:
        credentials = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PreflightError("SCRAPEYARD_API_CREDENTIALS is not valid JSON") from exc
    if not isinstance(credentials, dict) or not credentials:
        raise PreflightError("SCRAPEYARD_API_CREDENTIALS must be a non-empty object")
    matching = [
        spec
        for spec in credentials.values()
        if isinstance(spec, dict) and spec.get("secret") == probe_key
    ]
    if len(matching) != 1:
        raise PreflightError("health probe key must match exactly one named credential")
    probe = matching[0]
    if probe.get("scopes") != ["health-detail"] or probe.get("projects") is not None:
        raise PreflightError("health probe credential must be health-detail-only and unrestricted")
    operator_scopes = {
        scope
        for spec in credentials.values()
        if isinstance(spec, dict) and spec is not probe
        for scope in spec.get("scopes", [])
    }
    if not {"submit", "read"}.issubset(operator_scopes):
        raise PreflightError("production credentials need separate submit and read access")


def validate_encryption(environment: Mapping[str, Any]) -> None:
    raw = _required_env(environment, "SCRAPEYARD_ENCRYPTION_KEYS")
    active = _required_env(environment, "SCRAPEYARD_ENCRYPTION_ACTIVE_KEY_ID")
    try:
        keys = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PreflightError("SCRAPEYARD_ENCRYPTION_KEYS is not valid JSON") from exc
    if not isinstance(keys, dict) or active not in keys:
        raise PreflightError("active encryption key ID is absent from the keyring")
    for key_id, encoded in keys.items():
        if not isinstance(key_id, str) or not isinstance(encoded, str):
            raise PreflightError("encryption keyring entries must be strings")
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise PreflightError("encryption keyring contains invalid base64") from exc
        if len(decoded) != 32:
            raise PreflightError("each encryption key must decode to exactly 32 bytes")


def validate_compose(payload: Mapping[str, Any], image: str) -> None:
    services = payload.get("services")
    if not isinstance(services, dict):
        raise PreflightError("Compose configuration has no services")
    app = services.get("scrapeyard")
    redis = services.get("redis")
    egress_probe = services.get("egress-probe")
    if not all(isinstance(service, dict) for service in (app, redis, egress_probe)):
        raise PreflightError("Compose must define Scrapeyard, Redis, and the egress probe")
    assert isinstance(app, dict)
    assert isinstance(redis, dict)
    assert isinstance(egress_probe, dict)
    if app.get("image") != image:
        raise PreflightError("Compose application image differs from the immutable candidate")
    if egress_probe.get("image") != image:
        raise PreflightError("egress probe must use the scanned immutable candidate")
    probe_entrypoint = [str(value) for value in egress_probe.get("entrypoint") or []]
    probe_command = [str(value) for value in egress_probe.get("command") or []]
    if probe_entrypoint != ["python"] or not any(
        value.endswith("/egress_probe.py") for value in probe_command
    ):
        raise PreflightError("egress probe must run the controlled probe helper")
    if not egress_probe.get("read_only") or egress_probe.get("cap_drop") != ["ALL"]:
        raise PreflightError("egress probe privilege controls are missing")
    if app.get("ports"):
        raise PreflightError("production Scrapeyard ingress must remain internal-only")
    if redis.get("ports"):
        raise PreflightError("production Redis must not publish host ports")
    command = [str(value) for value in app.get("command") or []]
    if not any(
        value == "--workers=1"
        or (value == "--workers" and index + 1 < len(command) and command[index + 1] == "1")
        for index, value in enumerate(command)
    ):
        raise PreflightError("production server command must pin one worker process")
    deploy = app.get("deploy") or {}
    if int(deploy.get("replicas", 0)) != 1:
        raise PreflightError("production Compose must pin one replica")
    if not app.get("read_only") or app.get("cap_drop") != ["ALL"]:
        raise PreflightError("production application privilege controls are missing")
    security = set(app.get("security_opt") or [])
    required_security = {
        "no-new-privileges:true",
        "apparmor:scrapeyard-chromium",
        "seccomp:./security/seccomp/chromium.json",
    }
    if not required_security.issubset(security):
        raise PreflightError("production AppArmor/seccomp controls are missing")
    volumes = app.get("volumes") or []
    if not any(
        (isinstance(volume, str) and volume.endswith(":/data"))
        or (isinstance(volume, dict) and volume.get("target") == "/data")
        for volume in volumes
    ):
        raise PreflightError("production /data persistence is missing")
    if "backend" not in (app.get("networks") or {}) or "backend" not in (
        redis.get("networks") or {}
    ):
        raise PreflightError("application and private Redis must share the backend network")
    if "backend" not in (egress_probe.get("networks") or {}):
        raise PreflightError("egress probe must remain on the controlled backend network")

    environment = app.get("environment") or {}
    if not isinstance(environment, dict):
        raise PreflightError("application environment must be a mapping")
    validate_credentials(environment)
    validate_encryption(environment)
    proxy = _required_env(environment, "SCRAPEYARD_PROXY_URL")
    if proxy == "direct" or not re.match(r"^https?://", proxy):
        raise PreflightError("production requires a trustworthy HTTP(S) filtering proxy")
    if str(environment.get("SCRAPEYARD_UNTRUSTED_SUBMISSIONS", "")).lower() != "true":
        raise PreflightError("production must keep untrusted-submission controls enabled")
    for name in (
        "SCRAPEYARD_EGRESS_POLICY_PROBE_HOST",
        "SCRAPEYARD_EGRESS_POLICY_PROBE_PORT",
        "SCRAPEYARD_EGRESS_POLICY_PROBE_LIVENESS_PORT",
    ):
        _required_env(environment, name)
    health_test = " ".join(str(value) for value in (app.get("healthcheck") or {}).get("test", []))
    if "scrapeyard-entrypoint healthcheck" not in health_test:
        raise PreflightError("container health must use authenticated readiness")


def validate_host(sys_root: Path = Path("/")) -> None:
    if platform.system() != "Linux" or platform.machine().lower() not in {
        "x86_64",
        "amd64",
    }:
        raise PreflightError("production requires Linux/amd64")
    required_commands = ("docker", "iptables", "apparmor_parser")
    missing = [command for command in required_commands if shutil.which(command) is None]
    if missing:
        raise PreflightError(f"host commands are missing: {missing}")

    def read(relative: str) -> str:
        path = sys_root / relative.lstrip("/")
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise PreflightError(f"required host control is unavailable: {relative}") from exc

    if read("/sys/module/apparmor/parameters/enabled").lower() not in {"y", "yes", "1"}:
        raise PreflightError("AppArmor is not enabled")
    profiles = read("/sys/kernel/security/apparmor/profiles")
    if not any(line.startswith("scrapeyard-chromium ") for line in profiles.splitlines()):
        raise PreflightError("scrapeyard-chromium AppArmor profile is not loaded")
    userns_path = sys_root / "proc/sys/kernel/unprivileged_userns_clone"
    if userns_path.exists() and read(str(userns_path.relative_to(sys_root))) != "1":
        raise PreflightError("unprivileged user namespaces are disabled")
    if int(read("/proc/sys/user/max_user_namespaces")) <= 0:
        raise PreflightError("user namespace capacity is disabled")
    if read("/proc/sys/net/bridge/bridge-nf-call-iptables") != "1":
        raise PreflightError("bridge netfilter is not enabled")


def _json_output(command: list[str], *, environment: Mapping[str, str]) -> Any:
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
        env=environment,
    )
    return json.loads(completed.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default=os.environ.get("SCRAPEYARD_IMAGE", ""))
    parser.add_argument("--skip-host", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        image = args.image.strip()
        if not image or not (image.startswith("sha256:") or "@sha256:" in image):
            raise PreflightError("SCRAPEYARD_IMAGE must be an immutable digest reference")
        if not args.skip_host:
            validate_host()
        inspection = _json_output(
            ["docker", "image", "inspect", image],
            environment=os.environ,
        )[0]
        if inspection.get("Architecture") != "amd64" or inspection.get("Os") != "linux":
            raise PreflightError("candidate image is not Linux/amd64")
        compose = _json_output(
            ["docker", "compose", "config", "--format", "json"],
            environment={**os.environ, "SCRAPEYARD_IMAGE": image},
        )
        validate_compose(compose, image)
    except (
        OSError,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
        PreflightError,
    ) as exc:
        parser.error(str(exc))
    print("Production preflight passed: Linux/amd64, host policy, immutable image, and Compose contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
