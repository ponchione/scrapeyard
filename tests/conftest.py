"""Root test configuration — sets safe temp directories for all tests."""

import pytest


@pytest.fixture(autouse=True)
async def _scrapeyard_temp_dirs(tmp_path, monkeypatch):
    """Point all data directories to temp paths for every test."""
    monkeypatch.setenv("SCRAPEYARD_DB_DIR", str(tmp_path / "db"))
    monkeypatch.setenv("SCRAPEYARD_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("SCRAPEYARD_STORAGE_RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("SCRAPEYARD_ADAPTIVE_DIR", str(tmp_path / "adaptive"))

    from scrapeyard.common.settings import get_settings
    from scrapeyard.api.dependencies import reset_cached_dependencies
    from scrapeyard.storage.database import close_db

    get_settings.cache_clear()
    reset_cached_dependencies()

    yield

    await close_db()
    get_settings.cache_clear()
    reset_cached_dependencies()
