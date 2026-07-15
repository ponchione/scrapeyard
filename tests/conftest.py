"""Root test configuration — sets safe temp directories for all tests."""

import warnings

import pytest


@pytest.fixture(autouse=True)
async def _scrapeyard_temp_dirs(tmp_path, monkeypatch):
    """Point all data directories to temp paths for every test."""
    with warnings.catch_warnings():
        # Scrapling 0.2.x passes this no-op option to lxml 6.1.x. The dependency
        # bounds in pyproject.toml make this exact suppression temporary and
        # version-bounded; all other warnings remain errors in CI.
        warnings.filterwarnings(
            "ignore",
            message=(
                r"The 'strip_cdata' option of HTMLParser\(\) has never done "
                r"anything and will eventually be removed\."
            ),
            category=DeprecationWarning,
        )

        monkeypatch.setenv("SCRAPEYARD_DB_DIR", str(tmp_path / "db"))
        monkeypatch.setenv("SCRAPEYARD_LOG_DIR", str(tmp_path / "logs"))
        monkeypatch.setenv("SCRAPEYARD_STORAGE_RESULTS_DIR", str(tmp_path / "results"))
        monkeypatch.setenv("SCRAPEYARD_ADAPTIVE_DIR", str(tmp_path / "adaptive"))
        monkeypatch.setenv(
            "SCRAPEYARD_ENCRYPTION_KEYS",
            '{"test-v1":"MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="}',
        )
        monkeypatch.setenv("SCRAPEYARD_ENCRYPTION_ACTIVE_KEY_ID", "test-v1")

        from scrapeyard.api.dependencies import reset_cached_dependencies
        from scrapeyard.common.settings import get_settings
        from scrapeyard.storage.database import close_db

        get_settings.cache_clear()
        reset_cached_dependencies()

        yield

        await close_db()
        get_settings.cache_clear()
        reset_cached_dependencies()
