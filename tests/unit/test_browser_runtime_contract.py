from __future__ import annotations

from pathlib import Path


def test_dockerfile_installs_rebrowser_chromium_for_dynamic_stealth() -> None:
    dockerfile = Path("Dockerfile").read_text()

    assert "python -m playwright install --with-deps chromium" in dockerfile
    assert "python -m rebrowser_playwright install chromium" in dockerfile
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
