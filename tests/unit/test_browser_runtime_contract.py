from __future__ import annotations

import re
from pathlib import Path


def test_dockerfile_installs_one_chromium_for_dynamic_and_dynamic_stealth() -> None:
    dockerfile = Path("Dockerfile").read_text()

    assert "python -m playwright install --with-deps --no-shell chromium" in dockerfile
    # TD-45 spike, route U: no second driver installs a browser build.
    assert "rebrowser" not in dockerfile.lower()
    assert "ARG PLAYWRIGHT_CHROMIUM_REVISION=1208" in dockerfile
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
