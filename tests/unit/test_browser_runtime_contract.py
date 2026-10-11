from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


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


# The system calls numbered 428 to 462 on every Linux architecture, in order.
_NEWER_SYSCALLS = (
    "open_tree", "move_mount", "fsopen", "fsconfig", "fsmount", "fspick", "pidfd_open",
    "clone3", "close_range", "openat2", "pidfd_getfd", "faccessat2", "process_madvise",
    "epoll_pwait2", "mount_setattr", "quotactl_fd", "landlock_create_ruleset",
    "landlock_add_rule", "landlock_restrict_self", "memfd_secret", "process_mrelease",
    "futex_waitv", "set_mempolicy_home_node", "cachestat", "fchmodat2", "map_shadow_stack",
    "futex_wake", "futex_wait", "futex_requeue", "statmount", "listmount",
    "lsm_get_self_attr", "lsm_set_self_attr", "lsm_list_modules", "mseal",
)
# The 381 names of Playwright 1.58.0's list, none numbered above 427 on x86-64:
# sha256 of them sorted and joined by newlines.
_OLDER_SYSCALLS_SHA256 = "c1dc008431529a794c87715c0b8f8846a14c9106fc349a131fe79ca424f23e84"


def _seccomp_rules() -> dict[str, list[dict]]:
    profile = json.loads(Path("security/seccomp/chromium.json").read_text())
    assert profile["defaultAction"] == "SCMP_ACT_ERRNO" and "defaultErrnoRet" not in profile
    rules: dict[str, list[dict]] = {}
    for rule in profile["syscalls"]:
        for name in rule["names"]:
            rules.setdefault(name, []).append(rule)
    return rules


def _the_rule_that_always_applies(rules: dict[str, list[dict]], name: str) -> dict:
    # Docker drops a rule whose `includes` or `excludes` (a capability, an
    # architecture, a kernel version) do not hold, and `args` narrow one.
    assert len(rules[name]) == 1, name
    rule = rules[name][0]
    assert (rule["args"], rule["includes"], rule["excludes"]) == ([], {}, {}), name
    return rule


def test_seccomp_profile_lets_a_hardened_runc_start_the_container() -> None:
    # runc 1.3.3 opens /proc/thread-self/fd with openat2 after loading the
    # filter. Refused, neither a container nor an exec starts (2026-10-11).
    assert _the_rule_that_always_applies(_seccomp_rules(), "openat2")["action"] == "SCMP_ACT_ALLOW"


def test_seccomp_profile_names_no_system_call_this_test_cannot_place() -> None:
    # The test below needs each newer call's place in the kernel's numbering.
    # One it does not know (463 and up) goes into the tuple above, with every
    # call numbered below it, before it goes into the profile.
    older = sorted(set(_seccomp_rules()) - set(_NEWER_SYSCALLS))

    assert hashlib.sha256("\n".join(older).encode()).hexdigest() == _OLDER_SYSCALLS_SHA256


def test_seccomp_profile_answers_enosys_below_its_newest_syscall() -> None:
    # runc answers ENOSYS only above the highest-numbered system call a
    # profile names; one that is lower and not named gets the default EPERM.
    # glibc falls back from clone3 to clone on ENOSYS alone, so with openat2
    # named and clone3 left out, no thread could start.
    rules = _seccomp_rules()
    named = [name for name in _NEWER_SYSCALLS if name in rules]
    assert named, "the profile names none of the newer system calls"
    below_newest = _NEWER_SYSCALLS[: _NEWER_SYSCALLS.index(named[-1])]

    assert [name for name in below_newest if name not in rules] == []
    for name in below_newest:
        rule = _the_rule_that_always_applies(rules, name)
        assert rule["action"] == "SCMP_ACT_ALLOW" or (
            rule["action"] == "SCMP_ACT_ERRNO" and rule["errnoRet"] == 38
        ), name
    assert rules["clone3"][0]["action"] == "SCMP_ACT_ERRNO"
