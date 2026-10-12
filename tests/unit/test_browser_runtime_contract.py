from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from scripts import derive_seccomp_profile


def test_dockerfile_installs_rebrowser_chromium_for_dynamic_stealth() -> None:
    dockerfile = Path("Dockerfile").read_text()

    assert "python -m playwright install --with-deps --no-shell chromium" in dockerfile
    assert "python -m rebrowser_playwright install --no-shell chromium" in dockerfile
    assert "ARG PLAYWRIGHT_CHROMIUM_REVISION=1208" in dockerfile
    assert "ARG REBROWSER_CHROMIUM_REVISION=1169" in dockerfile
    assert "PLAYWRIGHT_BROWSERS_PATH=/ms-playwright" in dockerfile
    assert "XDG_CACHE_HOME=/opt/scrapeyard-cache" in dockerfile
    assert "HOME=/home/scrapeyard" in dockerfile
    assert "CHROME_DEVEL_SANDBOX" not in dockerfile
    assert "useradd --uid \"${SCRAPEYARD_UID}\"" in dockerfile
    assert "chmod 4755" not in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "setpriv" not in dockerfile
    assert "exec su scrapeyard" not in dockerfile


def test_dockerfile_installs_wheels_without_a_build_toolchain() -> None:
    dockerfile = Path("Dockerfile").read_text()

    assert "pip install --no-cache-dir --only-binary=:all: -r requirements.txt" in dockerfile
    for package in ("build-essential", "libxml2-dev", "libxslt1-dev"):
        assert package not in dockerfile


def test_runtime_image_removes_pip_after_its_last_install() -> None:
    dockerfile = Path("Dockerfile").read_text()
    runtime = dockerfile[dockerfile.index("AS runtime"):]
    uninstall = runtime.index("python -m pip uninstall --yes pip")

    assert runtime.rindex("pip install") < uninstall
    assert "! python -c 'import pip'" in runtime[uninstall:]
    # Nothing the image runs may need pip once it is gone.
    assert "pip" not in Path("scripts/container-entrypoint.sh").read_text().split()
    assert "pip" not in runtime[runtime.index("HEALTHCHECK"):]


def test_browser_driver_pins_match_the_versions_the_image_asserts() -> None:
    dockerfile = Path("Dockerfile").read_text()
    pyproject = Path("pyproject.toml").read_text()

    for package, arg in (
        ("playwright", "PLAYWRIGHT_VERSION"),
        ("rebrowser-playwright", "REBROWSER_PLAYWRIGHT_VERSION"),
        ("camoufox", "CAMOUFOX_PACKAGE_VERSION"),
    ):
        match = re.search(rf"(?m)^ARG {arg}=(\S+)$", dockerfile)
        assert match is not None
        assert f'"{package}=={match.group(1)}"' in pyproject


def test_docker_compose_enables_dynamic_stealth_sandbox_requirements() -> None:
    compose = Path("docker-compose.yml").read_text()

    assert "seccomp:unconfined" not in compose
    assert 'cap_drop: ["ALL"]' in compose
    assert 'cap_add: ["SYS_CHROOT"]' in compose
    assert "no-new-privileges:true" in compose
    assert "apparmor:scrapeyard-chromium" in compose
    assert "seccomp:./security/seccomp/chromium.json" in compose
    assert "read_only: true" in compose

    seccomp = Path("security/seccomp/chromium.json").read_text()
    assert '"clone"' in seccomp
    assert '"setns"' in seccomp
    assert '"unshare"' in seccomp
    apparmor = Path("security/apparmor/scrapeyard-chromium").read_text()
    assert "userns," in apparmor
    assert "deny mount," in apparmor


def test_docker_compose_is_private_and_local_override_is_loopback_only() -> None:
    compose = Path("docker-compose.yml").read_text()
    local = Path("docker-compose.local.yml").read_text()

    assert "ports:" not in compose
    assert '"${SCRAPEYARD_BIND_ADDRESS:-127.0.0.1}:${SCRAPEYARD_PORT:-8420}:8420"' in local


def test_dockerignore_excludes_local_env_files_from_build_context() -> None:
    dockerignore = Path(".dockerignore").read_text().splitlines()

    assert ".env" in dockerignore
    assert ".env.*" in dockerignore
    assert "!.env.example" in dockerignore


def test_dockerignore_excludes_local_state_and_nested_caches() -> None:
    dockerignore = Path(".dockerignore").read_text().splitlines()

    for pattern in (
        "artifacts/",
        ".artifacts/",
        ".worktrees/",
        "data/",
        "**/*.db",
        "**/__pycache__",
        "**/*.pyc",
    ):
        assert pattern in dockerignore


def test_readme_documents_dynamic_stealth_runtime_and_rebuild_flow() -> None:
    readme = Path("README.md").read_text()

    assert "SCRAPEYARD_BIND_ADDRESS=0.0.0.0" in readme
    assert "docker-compose.local.yml" in readme
    assert "rebrowser Chromium" in readme
    assert "browser.stealth: true" in readme
    assert "up -d --build --force-recreate scrapeyard" in readme
    assert "non-root" in readme
    assert "sandbox" in readme
    assert "seccomp" in readme
    assert "volume" in readme
    assert "ownership" in readme


# github.com/moby/profiles/seccomp v0.2.4, as Docker 29.9.0 vendors it.
_MOBY_DEFAULT_SHA256 = "785b2429264afba4d594320337cb17f144f3c7d51585f9805eef72e28f4f9334"
# docker-compose.yml drops every capability and adds this one, and Docker
# reads a rule's capabilities against the container's bounding set.
_BOUNDING_SET = {"CAP_SYS_CHROOT"}


def _rules_in_the_production_container(name: str) -> list[dict]:
    """The rules Docker keeps for one system call: the image is amd64 only."""
    profile = json.loads(Path("security/seccomp/chromium.json").read_text())
    assert (profile["defaultAction"], profile["defaultErrnoRet"]) == ("SCMP_ACT_ERRNO", 1)
    kept = []
    for rule in profile["syscalls"]:
        # Docker reads keys without regard to case, so `Includes` beside
        # `includes` would be a condition this test does not see.
        assert set(rule) <= {"comment", "names", "action", "args", "includes", "excludes", "errnoRet"}
        includes, excludes = rule.get("includes", {}), rule.get("excludes", {})
        assert set(includes) <= {"arches", "caps", "minKernel"} and set(excludes) <= {"arches", "caps"}
        if name not in rule["names"]:
            continue
        if "amd64" in excludes.get("arches", []) or _BOUNDING_SET & set(excludes.get("caps", [])):
            continue
        if "arches" in includes and "amd64" not in includes["arches"]:
            continue
        if not set(includes.get("caps", [])) <= _BOUNDING_SET:
            continue
        kept.append(rule)
    return kept


def test_seccomp_profile_is_mobys_default_plus_the_user_namespace_rule() -> None:
    # The whole difference from what every Docker container gets is the one
    # rule in the script. Anything else is a change to review as one: move to
    # a newer default as security/seccomp/README.md describes.
    moby_default = Path("security/seccomp/moby-default.json").read_bytes()

    assert hashlib.sha256(moby_default).hexdigest() == _MOBY_DEFAULT_SHA256
    assert Path("security/seccomp/chromium.json").read_text() == derive_seccomp_profile.derive(
        moby_default.decode()
    )
    assert derive_seccomp_profile.USER_NAMESPACE_RULE == {
        "comment": "Chromium's sandbox creates user namespaces without CAP_SYS_ADMIN",
        "names": ["clone", "setns", "unshare"],
        "action": "SCMP_ACT_ALLOW",
    }


def test_seccomp_profile_lets_a_hardened_runc_start_the_container() -> None:
    # runc 1.3.3 opens /proc/thread-self/fd with openat2 after loading the
    # filter. Refused, neither a container nor an exec starts (2026-10-11).
    assert {"action": "SCMP_ACT_ALLOW"} in [
        {key: rule[key] for key in rule if key not in ("names", "comment")}
        for rule in _rules_in_the_production_container("openat2")
    ]


def test_seccomp_profile_answers_enosys_to_clone3() -> None:
    # glibc falls back from clone3 to clone on ENOSYS alone: with EPERM no
    # thread starts. A filter cannot read the flags clone3 is passed, so the
    # profile leaves it as Moby has it and does not allow it.
    (rule,) = _rules_in_the_production_container("clone3")

    assert (rule["action"], rule["errnoRet"]) == ("SCMP_ACT_ERRNO", 38)
    assert "args" not in rule


def test_seccomp_profile_keeps_closed_what_mobys_default_closes() -> None:
    # The profile this one replaced was Moby's of 2020 and still allowed
    # io_uring and every socket family.
    for name in ("io_uring_setup", "io_uring_enter", "io_uring_register"):
        assert _rules_in_the_production_container(name) == []

    def admits(family: int) -> bool:
        compare = {
            "SCMP_CMP_LT": family.__lt__,
            "SCMP_CMP_EQ": family.__eq__,
            "SCMP_CMP_NE": family.__ne__,
        }
        admitted = False
        for rule in _rules_in_the_production_container("socket"):
            assert rule["action"] == "SCMP_ACT_ALLOW"
            # No rule without a condition, and each condition is on the family.
            assert rule.get("args") and all(arg["index"] == 0 for arg in rule["args"])
            admitted |= all(compare[arg["op"]](arg["value"]) for arg in rule["args"])
        return admitted

    assert admits(1) and admits(2) and admits(10) and admits(16)  # unix, inet, inet6, netlink
    assert not admits(38)  # AF_ALG: the kernel's crypto interface
    assert not admits(40)  # AF_VSOCK: the hypervisor's sockets
