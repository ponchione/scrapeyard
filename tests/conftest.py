"""Root test configuration — sets safe temp directories for all tests."""

import pytest


@pytest.fixture(autouse=True)
def _scrapeyard_temp_dirs(tmp_path, monkeypatch):
    """Point all data directories to temp paths for every test."""
    monkeypatch.setenv("SCRAPEYARD_DB_DIR", str(tmp_path / "db"))
    monkeypatch.setenv("SCRAPEYARD_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("SCRAPEYARD_STORAGE_RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("SCRAPEYARD_ADAPTIVE_DIR", str(tmp_path / "adaptive"))

    from scrapeyard.common.settings import get_settings
    from scrapeyard.api.dependencies import (
        get_circuit_breaker,
        get_error_store,
        get_job_store,
        get_result_store,
        get_scheduler,
        get_worker_pool,
    )

    # Clear all cached singletons.
    for cached_fn in [
        get_settings,
        get_job_store,
        get_error_store,
        get_result_store,
        get_circuit_breaker,
        get_worker_pool,
        get_scheduler,
    ]:
        cached_fn.cache_clear()

    yield

    for cached_fn in [
        get_settings,
        get_job_store,
        get_error_store,
        get_result_store,
        get_circuit_breaker,
        get_worker_pool,
        get_scheduler,
    ]:
        cached_fn.cache_clear()
