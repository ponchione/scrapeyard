from __future__ import annotations

import base64
import copy
import json
from pathlib import Path

import pytest

from scripts.production_preflight import (
    PreflightError,
    validate_compose,
    validate_host,
)


IMAGE = "registry.example/scrapeyard@sha256:" + "a" * 64


def _compose() -> dict:
    operator_key = "operator-key-000000000000000"
    health_key = "health-probe-key-000000000000"
    credentials = json.dumps(
        {
            "operator": {
                "secret": operator_key,
                "scopes": ["submit", "read", "schedule-admin", "delete"],
            },
            "health": {"secret": health_key, "scopes": ["health-detail"]},
        }
    )
    keyring = json.dumps(
        {"v1": base64.b64encode(b"0" * 32).decode("ascii")}
    )
    return {
        "services": {
            "scrapeyard": {
                "image": IMAGE,
                "command": ["uvicorn", "scrapeyard.main:app", "--workers", "1"],
                "deploy": {"replicas": 1},
                "read_only": True,
                "cap_drop": ["ALL"],
                "security_opt": [
                    "no-new-privileges:true",
                    "apparmor:scrapeyard-chromium",
                    "seccomp:./security/seccomp/chromium.json",
                ],
                "volumes": ["scrapeyard-data:/data"],
                "networks": {"backend": {}},
                "healthcheck": {
                    "test": [
                        "CMD",
                        "/usr/local/bin/scrapeyard-entrypoint",
                        "healthcheck",
                    ]
                },
                "environment": {
                    "SCRAPEYARD_API_CREDENTIALS": credentials,
                    "SCRAPEYARD_LOCAL_DEVELOPMENT_UNAUTHENTICATED": "false",
                    "SCRAPEYARD_HEALTH_PROBE_API_KEY": health_key,
                    "SCRAPEYARD_ENCRYPTION_KEYS": keyring,
                    "SCRAPEYARD_ENCRYPTION_ACTIVE_KEY_ID": "v1",
                    "SCRAPEYARD_PROXY_URL": "https://proxy.example:8443",
                    "SCRAPEYARD_UNTRUSTED_SUBMISSIONS": "true",
                    "SCRAPEYARD_EGRESS_POLICY_PROBE_HOST": "172.30.0.248",
                    "SCRAPEYARD_EGRESS_POLICY_PROBE_PORT": "8080",
                    "SCRAPEYARD_EGRESS_POLICY_PROBE_LIVENESS_PORT": "8081",
                },
            },
            "redis": {
                "networks": {"backend": {}},
            },
            "egress-probe": {
                "image": IMAGE,
                "entrypoint": ["python"],
                "command": [
                    "/opt/scrapeyard/egress_probe.py",
                    "--challenge-port",
                    "8080",
                    "--liveness-port",
                    "8081",
                ],
                "read_only": True,
                "cap_drop": ["ALL"],
                "networks": {"backend": {}},
            },
        }
    }


def _set(payload: dict, path: str, value) -> dict:
    updated = copy.deepcopy(payload)
    target = updated
    parts = path.split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value
    return updated


def test_complete_production_compose_preflight_passes():
    validate_compose(_compose(), IMAGE)


@pytest.mark.parametrize(
    ("path", "value", "match"),
    [
        ("services.scrapeyard.image", "scrapeyard:mutable", "immutable candidate"),
        ("services.egress-probe.image", "scrapeyard:other", "scanned immutable"),
        ("services.egress-probe.entrypoint", ["sh"], "controlled probe"),
        ("services.egress-probe.read_only", False, "probe privilege"),
        ("services.egress-probe.networks", {}, "controlled backend"),
        ("services.scrapeyard.ports", ["8420:8420"], "internal-only"),
        ("services.redis.ports", ["6379:6379"], "must not publish"),
        ("services.scrapeyard.command", ["uvicorn", "--workers", "2"], "one worker"),
        ("services.scrapeyard.deploy.replicas", 2, "one replica"),
        ("services.scrapeyard.read_only", False, "privilege controls"),
        ("services.scrapeyard.cap_drop", [], "privilege controls"),
        ("services.scrapeyard.security_opt", [], "AppArmor/seccomp"),
        ("services.scrapeyard.volumes", [], "/data persistence"),
        ("services.scrapeyard.networks", {}, "private Redis"),
        ("services.redis.networks", {}, "private Redis"),
        (
            "services.scrapeyard.environment.SCRAPEYARD_LOCAL_DEVELOPMENT_UNAUTHENTICATED",
            "true",
            "disable local",
        ),
        (
            "services.scrapeyard.environment.SCRAPEYARD_HEALTH_PROBE_API_KEY",
            "wrong-health-key-00000000000",
            "exactly one",
        ),
        (
            "services.scrapeyard.environment.SCRAPEYARD_ENCRYPTION_ACTIVE_KEY_ID",
            "missing",
            "absent",
        ),
        (
            "services.scrapeyard.environment.SCRAPEYARD_PROXY_URL",
            "direct",
            "filtering proxy",
        ),
        (
            "services.scrapeyard.environment.SCRAPEYARD_UNTRUSTED_SUBMISSIONS",
            "false",
            "untrusted-submission",
        ),
        (
            "services.scrapeyard.environment.SCRAPEYARD_EGRESS_POLICY_PROBE_HOST",
            "",
            "is required",
        ),
        ("services.scrapeyard.healthcheck.test", ["CMD", "true"], "readiness"),
    ],
)
def test_each_local_production_contract_fails_closed(path, value, match):
    with pytest.raises(PreflightError, match=match):
        validate_compose(_set(_compose(), path, value), IMAGE)


def _host_tree(root: Path) -> None:
    values = {
        "sys/module/apparmor/parameters/enabled": "Y\n",
        "sys/kernel/security/apparmor/profiles": (
            "scrapeyard-chromium (enforce)\n"
        ),
        "proc/sys/kernel/unprivileged_userns_clone": "1\n",
        "proc/sys/user/max_user_namespaces": "1024\n",
        "proc/sys/net/bridge/bridge-nf-call-iptables": "1\n",
    }
    for name, value in values.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")


def test_linux_amd64_host_policy_contract_passes(monkeypatch, tmp_path):
    _host_tree(tmp_path)
    monkeypatch.setattr("scripts.production_preflight.platform.system", lambda: "Linux")
    monkeypatch.setattr("scripts.production_preflight.platform.machine", lambda: "x86_64")
    monkeypatch.setattr(
        "scripts.production_preflight.shutil.which",
        lambda command: f"/usr/bin/{command}",
    )

    validate_host(tmp_path)


@pytest.mark.parametrize(
    ("path", "value", "match"),
    [
        ("sys/module/apparmor/parameters/enabled", "N\n", "AppArmor"),
        ("sys/kernel/security/apparmor/profiles", "other (enforce)\n", "not loaded"),
        ("proc/sys/kernel/unprivileged_userns_clone", "0\n", "namespaces"),
        ("proc/sys/user/max_user_namespaces", "0\n", "capacity"),
        ("proc/sys/net/bridge/bridge-nf-call-iptables", "0\n", "netfilter"),
    ],
)
def test_host_kernel_requirements_fail_closed(monkeypatch, tmp_path, path, value, match):
    _host_tree(tmp_path)
    (tmp_path / path).write_text(value, encoding="utf-8")
    monkeypatch.setattr("scripts.production_preflight.platform.system", lambda: "Linux")
    monkeypatch.setattr("scripts.production_preflight.platform.machine", lambda: "x86_64")
    monkeypatch.setattr(
        "scripts.production_preflight.shutil.which",
        lambda command: f"/usr/bin/{command}",
    )

    with pytest.raises(PreflightError, match=match):
        validate_host(tmp_path)


def test_non_linux_amd64_host_fails(monkeypatch, tmp_path):
    _host_tree(tmp_path)
    monkeypatch.setattr("scripts.production_preflight.platform.system", lambda: "Darwin")
    monkeypatch.setattr("scripts.production_preflight.platform.machine", lambda: "arm64")

    with pytest.raises(PreflightError, match="Linux/amd64"):
        validate_host(tmp_path)
