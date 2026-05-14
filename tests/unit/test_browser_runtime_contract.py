from __future__ import annotations

from pathlib import Path


def test_dockerfile_installs_rebrowser_chromium_for_dynamic_stealth() -> None:
    dockerfile = Path("Dockerfile").read_text()

    assert "python -m playwright install --with-deps chromium" in dockerfile
    assert "python -m rebrowser_playwright install chromium" in dockerfile
    assert 'PLAYWRIGHT_BROWSERS_PATH="/ms-playwright"' in dockerfile
    assert 'XDG_CACHE_HOME="/var/cache/scrapeyard"' in dockerfile
    assert 'CHROME_DEVEL_SANDBOX="/ms-playwright/chromium-1169/chrome-linux/chrome_sandbox"' in dockerfile
    assert 'useradd --create-home --home-dir /home/scrapeyard --shell /bin/bash scrapeyard' in dockerfile
    assert "chown root:root /ms-playwright/chromium-1169/chrome-linux/chrome_sandbox" in dockerfile
    assert "chmod 4755 /ms-playwright/chromium-1169/chrome-linux/chrome_sandbox" in dockerfile
    assert "exec su scrapeyard -s /bin/sh -c 'uvicorn scrapeyard.main:app --host 0.0.0.0 --port 8420'" in dockerfile


def test_docker_compose_enables_dynamic_stealth_sandbox_requirements() -> None:
    compose = Path("docker-compose.yml").read_text()

    assert 'security_opt:' in compose
    assert 'seccomp:unconfined' in compose


def test_dockerignore_excludes_local_env_files_from_build_context() -> None:
    dockerignore = Path(".dockerignore").read_text().splitlines()

    assert ".env" in dockerignore
    assert ".env.*" in dockerignore
    assert "!.env.example" in dockerignore


def test_readme_documents_dynamic_stealth_runtime_and_rebuild_flow() -> None:
    readme = Path("README.md").read_text()

    assert "rebrowser Chromium" in readme
    assert "browser.stealth: true" in readme
    assert "docker compose up -d --build --force-recreate scrapeyard" in readme
    assert "non-root" in readme
    assert "sandbox" in readme
    assert "seccomp" in readme
    assert "volume" in readme
    assert "ownership" in readme
