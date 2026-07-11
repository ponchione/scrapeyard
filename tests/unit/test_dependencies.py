from scrapeyard.api.dependencies import (
    get_circuit_breaker,
    get_error_store,
    get_job_store,
    get_result_store,
    get_scheduler,
    get_webhook_dispatcher,
    get_webhook_outbox_store,
    get_worker_pool,
    init_rate_limiter,
    reset_cached_dependencies,
)
from scrapeyard.common.settings import get_settings


def test_reset_cached_dependencies_clears_cached_singletons_and_rate_limiter():
    get_job_store()
    get_error_store()
    get_result_store()
    get_circuit_breaker()
    get_webhook_outbox_store()
    get_webhook_dispatcher()
    get_worker_pool()
    get_scheduler()
    init_rate_limiter()

    assert get_job_store.cache_info().currsize == 1
    assert get_webhook_outbox_store.cache_info().currsize == 1
    assert get_scheduler.cache_info().currsize == 1

    reset_cached_dependencies()

    assert get_job_store.cache_info().currsize == 0
    assert get_error_store.cache_info().currsize == 0
    assert get_result_store.cache_info().currsize == 0
    assert get_circuit_breaker.cache_info().currsize == 0
    assert get_webhook_outbox_store.cache_info().currsize == 0
    assert get_webhook_dispatcher.cache_info().currsize == 0
    assert get_worker_pool.cache_info().currsize == 0
    assert get_scheduler.cache_info().currsize == 0


def test_result_store_cache_reset_applies_changed_reconciliation_environment(
    monkeypatch, tmp_path
):
    first = tmp_path / "first"
    second = tmp_path / "second"
    monkeypatch.setenv("SCRAPEYARD_STORAGE_RESULTS_DIR", str(first))
    get_settings.cache_clear()
    reset_cached_dependencies()
    first_store = get_result_store()

    monkeypatch.setenv("SCRAPEYARD_STORAGE_RESULTS_DIR", str(second))
    get_settings.cache_clear()
    reset_cached_dependencies()
    second_store = get_result_store()

    assert first_store._results_dir == first
    assert second_store._results_dir == second
    assert second_store._active_run_lookup is not None
