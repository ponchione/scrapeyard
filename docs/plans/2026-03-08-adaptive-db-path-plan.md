# Adaptive DB Path Configuration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Configure Scrapling to store its adaptive fingerprint database at the path from `ServiceSettings.adaptive_dir`.

**Architecture:** Add an `adaptive_dir` parameter to `scrape_target` and `_fetch_page` in `scraper.py`. Pass `storage_args` with the configured path in the Scrapling `custom_config`. The worker passes `adaptive_dir` from settings. The directory is created if it doesn't exist.

**Tech Stack:** Scrapling, pytest, unittest.mock

---

### Task 1: Write the failing test

**Files:**
- Create: `tests/unit/test_scraper_adaptive.py`

**Step 1: Write the test**

Create `tests/unit/test_scraper_adaptive.py`:

```python
"""Test that Scrapling adaptive DB path is configured correctly."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scrapeyard.config.schema import FetcherType, RetryConfig, TargetConfig
from scrapeyard.engine.scraper import scrape_target


@pytest.mark.asyncio
async def test_adaptive_db_path_passed_to_fetcher(tmp_path):
    """Verify that the adaptive DB path is passed via storage_args."""
    adaptive_dir = tmp_path / "adaptive"

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.css.return_value = []

    target = TargetConfig(
        url="http://example.com",
        fetcher=FetcherType.basic,
        selectors={"title": "h1"},
    )
    retry = RetryConfig()

    with patch("scrapeyard.engine.scraper.Fetcher") as mock_fetcher:
        mock_fetcher.get.return_value = mock_response
        await scrape_target(target, adaptive=True, retry=retry, adaptive_dir=str(adaptive_dir))

        # Verify Fetcher.get was called with custom_config containing storage_args.
        call_kwargs = mock_fetcher.get.call_args
        custom_config = call_kwargs.kwargs.get("custom_config") or call_kwargs[1].get("custom_config")
        assert custom_config["auto_match"] is True
        assert custom_config["storage_args"]["storage_file"] == str(adaptive_dir / "scrapling.db")

    # Verify the directory was created.
    assert adaptive_dir.exists()


@pytest.mark.asyncio
async def test_adaptive_dir_created_if_missing(tmp_path):
    """Verify that the adaptive_dir is created if it does not exist."""
    adaptive_dir = tmp_path / "does_not_exist" / "adaptive"
    assert not adaptive_dir.exists()

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.css.return_value = []

    target = TargetConfig(
        url="http://example.com",
        fetcher=FetcherType.basic,
        selectors={"title": "h1"},
    )
    retry = RetryConfig()

    with patch("scrapeyard.engine.scraper.Fetcher") as mock_fetcher:
        mock_fetcher.get.return_value = mock_response
        await scrape_target(target, adaptive=True, retry=retry, adaptive_dir=str(adaptive_dir))

    assert adaptive_dir.exists()


@pytest.mark.asyncio
async def test_adaptive_false_still_passes_storage_args(tmp_path):
    """Even when adaptive=False, storage_args should be set for consistency."""
    adaptive_dir = tmp_path / "adaptive"

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.css.return_value = []

    target = TargetConfig(
        url="http://example.com",
        fetcher=FetcherType.basic,
        selectors={"title": "h1"},
    )
    retry = RetryConfig()

    with patch("scrapeyard.engine.scraper.Fetcher") as mock_fetcher:
        mock_fetcher.get.return_value = mock_response
        await scrape_target(target, adaptive=False, retry=retry, adaptive_dir=str(adaptive_dir))

        call_kwargs = mock_fetcher.get.call_args
        custom_config = call_kwargs.kwargs.get("custom_config") or call_kwargs[1].get("custom_config")
        assert custom_config["auto_match"] is False
        assert "storage_args" in custom_config
```

**Step 2: Run tests to verify they fail**

Run: `source .venv/bin/activate && pytest tests/unit/test_scraper_adaptive.py -v`
Expected: FAIL — `TypeError: scrape_target() got an unexpected keyword argument 'adaptive_dir'`

**Step 3: Commit**

```bash
git add tests/unit/test_scraper_adaptive.py
git commit -m "test: add tests for adaptive DB path configuration"
```

---

### Task 2: Implement the adaptive_dir parameter in scraper.py

**Files:**
- Modify: `src/scrapeyard/engine/scraper.py`

**Step 1: Update `_fetch_page` to accept and use `adaptive_dir`**

In `src/scrapeyard/engine/scraper.py`, modify the `_fetch_page` function signature and body.

Change `_fetch_page` (lines 46-76) to:

```python
async def _fetch_page(
    fetcher_cls: Any,
    url: str,
    fetcher_type: FetcherType,
    adaptive: bool,
    retryable_status: set[int],
    adaptive_dir: str,
) -> Any:
    """Fetch a single page using the appropriate Scrapling method.

    Raises :class:`RetryableError` for retryable HTTP status codes,
    :class:`FetchError` for other error statuses.
    """
    custom_config: dict[str, Any] = {
        "auto_match": adaptive,
        "storage_args": {
            "storage_file": str(Path(adaptive_dir) / "scrapling.db"),
            "url": url,
        },
    }

    if fetcher_type == FetcherType.basic:
        # Fetcher.get is synchronous — run in thread pool.
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: fetcher_cls.get(url, custom_config=custom_config),
        )
    else:
        # StealthyFetcher / PlayWrightFetcher have async_fetch.
        response = await fetcher_cls.async_fetch(url, custom_config=custom_config)

    if response.status and response.status >= 400:
        if response.status in retryable_status:
            raise RetryableError(response.status)
        raise FetchError(response.status)

    return response
```

Note: Add `from pathlib import Path` to the imports at the top of the file.

**Step 2: Update `scrape_target` to accept `adaptive_dir` and create the directory**

Change the `scrape_target` function signature to add `adaptive_dir: str`:

```python
async def scrape_target(
    target: TargetConfig,
    adaptive: bool,
    retry: RetryConfig,
    adaptive_dir: str = "/data/adaptive",
) -> TargetResult:
```

Add directory creation at the start of the function body (after `result = TargetResult(url=target.url)`):

```python
    result = TargetResult(url=target.url)
    fetcher_cls = _get_fetcher(target.fetcher)
    retry_handler = RetryHandler(retry)
    retryable_status = set(retry.retryable_status)

    # Ensure adaptive directory exists.
    Path(adaptive_dir).mkdir(parents=True, exist_ok=True)
```

Update both `_fetch_page` calls in `scrape_target` to pass `adaptive_dir`:

Line ~102 (first fetch):
```python
        page = await retry_handler.execute(
            _fetch_page, fetcher_cls, target.url, target.fetcher, adaptive, retryable_status, adaptive_dir
        )
```

Line ~121 (pagination fetch):
```python
                page = await retry_handler.execute(
                    _fetch_page, fetcher_cls, next_url, target.fetcher, adaptive, retryable_status, adaptive_dir
                )
```

**Step 3: Run tests**

Run: `source .venv/bin/activate && pytest tests/unit/test_scraper_adaptive.py -v`
Expected: All 3 PASS

**Step 4: Commit**

```bash
git add src/scrapeyard/engine/scraper.py
git commit -m "feat: configure Scrapling adaptive DB path from settings"
```

---

### Task 3: Update worker.py to pass adaptive_dir

**Files:**
- Modify: `src/scrapeyard/queue/worker.py:95`

**Step 1: Update the scrape_target call in worker.py**

In `src/scrapeyard/queue/worker.py`, add an import for `get_settings` at the top (if not already there) and update the `scrape_target` call.

Add to imports:
```python
from scrapeyard.common.settings import get_settings
```

Change line 95 from:
```python
            result = await scrape_target(target_cfg, adaptive, config.retry)
```
To:
```python
            settings = get_settings()
            result = await scrape_target(target_cfg, adaptive, config.retry, adaptive_dir=settings.adaptive_dir)
```

**Step 2: Run ruff**

Run: `source .venv/bin/activate && ruff check src/scrapeyard/engine/ src/scrapeyard/queue/worker.py`
Expected: All checks passed

**Step 3: Run full test suite**

Run: `source .venv/bin/activate && pytest tests/ -v`
Expected: All PASS

**Step 4: Commit**

```bash
git add src/scrapeyard/queue/worker.py
git commit -m "feat: pass adaptive_dir from settings to scrape_target"
```

---

### Task 4: Lint and final verification

**Files:** None (verification only)

**Step 1: Run ruff on engine directory**

Run: `source .venv/bin/activate && ruff check src/scrapeyard/engine/`
Expected: All checks passed

**Step 2: Run full test suite**

Run: `source .venv/bin/activate && pytest tests/ -v`
Expected: All PASS

**Step 3: Commit if any fixes were needed**

```bash
git add -u
git commit -m "chore: lint fixes for adaptive DB path"
```
